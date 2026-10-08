"""The gateway serving the Gemini API (`POST /v1beta/models/{model}:generateContent` and
`:streamGenerateContent`), as Gemini CLI and Google's SDKs call it."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from seatbelt.attest.sign import Signer
from seatbelt.gateway.app import create_app
from seatbelt.gateway.config import add_principal, load_config
from seatbelt.gateway.sessions import Sessions
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import read_events

FIXTURE = Path(__file__).parent.parent / "fixtures" / "gemini_refund.json"
REAL = "AIza-REAL-0000000000000000000000000"  # a fake provider key
MODEL = "gemini-2.5-pro"
GENERATE = f"/v1beta/models/{MODEL}:generateContent"
STREAM = f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
SSE_HEADERS = {"content-type": "text/event-stream"}


def _replies() -> list[dict[str, Any]]:  # fixture JSON
    return json.loads(FIXTURE.read_text())


@dataclass
class Gw:
    client: TestClient
    app: Starlette
    key: str
    ledgers: Path
    sessions: Sessions
    seen: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def upstream(self, *replies: httpx2.Response) -> None:
        queue = list(replies)

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.seen.append(request)
            return queue.pop(0)

        self.app.state.transport = httpx2.MockTransport(handler)

    def post(self, body: dict[str, Any], path: str = GENERATE, **headers: str) -> httpx2.Response:
        return self.client.post(path, json=body, headers={"x-goog-api-key": self.key, **headers})

    def events(self) -> list[Event]:
        return [e for p in sorted(self.ledgers.glob("*.jsonl")) for e in read_events(p)]


def _gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str) -> Iterator[Gw]:
    monkeypatch.setenv("GEMINI_API_KEY", REAL)
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "ledgers: runs\nupstreams:\n"
        "  gemini: {url: https://generativelanguage.googleapis.com, key_env: GEMINI_API_KEY}\n"
        + extra
    )
    key = add_principal(path, "alice@corp")
    cfg = load_config(path)
    sessions = Sessions(cfg.ledgers, Signer.generate(), idle=900)
    app = create_app(cfg, sessions)
    with TestClient(app) as client:
        yield Gw(client, app, key, cfg.ledgers, sessions)
    assert sessions.close_all(timeout=1) == 0


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gw]:
    yield from _gateway(tmp_path, monkeypatch, "")


@pytest.fixture
def gwp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gw]:
    policy = "policy:\n  models: [gemini-2.5-pro]\n  max_output_tokens: 1000\n"
    yield from _gateway(tmp_path, monkeypatch, policy + "  tools_denied: [lookup_order]\n")


def _body(*contents: dict[str, Any], **extra: Any) -> dict[str, Any]:  # request JSON
    return {"contents": [{"role": "user", "parts": [{"text": "refund 1001"}]}, *contents], **extra}


def test_generate_content_is_routed_to_gemini_and_recorded(gw: Gw) -> None:
    first, _ = _replies()
    gw.upstream(httpx2.Response(200, json=first))
    r = gw.post(_body(), **{"x-seatbelt-run": "gemini-1a2b"})
    assert r.status_code == 200 and r.json() == first
    (sent,) = gw.seen
    assert str(sent.url) == f"https://generativelanguage.googleapis.com{GENERATE}"
    assert sent.headers["x-goog-api-key"] == REAL
    assert "x-seatbelt-run" not in sent.headers and gw.key not in str(sent.headers)
    events = gw.events()
    assert [e.kind for e in events] == [
        Kind.RUN_START,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
    ]
    assert events[0].attrs["run.name"] == "gemini-1a2b"
    assert events[1].attrs["gen_ai.request.model"] == MODEL  # from the URL
    assert events[1].attrs["gen_ai.provider.name"] == "gemini"
    assert events[2].attrs["gen_ai.usage.output_tokens"] == 31


def _sse(chunks: list[dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks).encode()


def test_a_stream_is_relayed_as_sent_and_recorded_whole(gw: Gw) -> None:
    first, _ = _replies()
    thought, call = first["candidates"][0]["content"]["parts"]
    chunks = [
        {"candidates": [{"content": {"role": "model", "parts": [thought]}, "index": 0}]},
        {**first, "candidates": [{**first["candidates"][0], "content": {"parts": [call]}}]},
    ]
    raw = _sse(chunks)
    gw.upstream(httpx2.Response(200, headers=SSE_HEADERS, content=raw))
    r = gw.post(_body(), path=STREAM)
    assert r.status_code == 200 and r.content == raw
    assert gw.seen[0].url.query == b"alt=sse"
    (response,) = [e for e in gw.events() if e.kind is Kind.MODEL_RESPONSE]
    assert response.attrs["error"] is None
    assert response.attrs["gen_ai.response"]["candidates"][0]["content"]["parts"] == [
        thought,
        call,
    ]
    assert [e.attrs["gen_ai.tool.name"] for e in gw.events() if e.kind is Kind.TOOL_CALL] == [
        "lookup_order"
    ]


def test_an_error_on_a_stream_request_is_relayed_and_recorded(gw: Gw) -> None:
    """Google answers a failed SSE request with plain JSON under an SSE content type."""
    error = {"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}}
    gw.upstream(httpx2.Response(400, headers=SSE_HEADERS, content=json.dumps(error).encode()))
    r = gw.post(_body(), path=STREAM)
    assert r.status_code == 400 and r.json() == error
    (response,) = [e for e in gw.events() if e.kind is Kind.MODEL_RESPONSE]
    assert response.attrs["error"].startswith("400: ")


def test_a_json_array_stream_is_recorded_whole(gw: Gw) -> None:
    """streamGenerateContent without alt=sse answers with a JSON array of chunks."""
    first, _ = _replies()
    gw.upstream(httpx2.Response(200, json=[first]))
    r = gw.post(_body(), path=f"/v1beta/models/{MODEL}:streamGenerateContent")
    assert r.status_code == 200 and r.json() == [first]
    kinds = [e.kind for e in gw.events()]
    assert kinds[-2:] == [Kind.MODEL_RESPONSE, Kind.TOOL_CALL]


def test_a_key_given_as_a_parameter_is_accepted_and_goes_no_further(gw: Gw) -> None:
    first, _ = _replies()
    gw.upstream(httpx2.Response(200, json=first))
    r = gw.client.post(f"{STREAM}&key={gw.key}", json=_body())
    assert r.status_code == 200
    gw.upstream(httpx2.Response(200, json=first))
    spelled = gw.post(_body(), path=f"{STREAM}&%6Bey={gw.key}&%24key=AIza-own&x=1")
    assert spelled.status_code == 200
    for sent in gw.seen:  # no key parameter, however spelled, reaches Google
        assert sent.url.query in (b"alt=sse", b"alt=sse&x=1")
        assert sent.headers["x-goog-api-key"] == REAL


def test_errors_are_shaped_as_google_clients_read_them(gw: Gw) -> None:
    gw.upstream(*(httpx2.Response(200, json={}) for _ in range(5)))  # would catch a leak
    r = gw.client.post(GENERATE, json=_body(), headers={"x-goog-api-key": "AIza-not-ours"})
    assert r.status_code == 401
    assert r.json() == {
        "error": {"code": 401, "message": "unknown seatbelt key", "status": "UNAUTHENTICATED"}
    }
    v1 = gw.client.post(f"/v1/models/{MODEL}:generateContent", json=_body())  # no key at all
    assert v1.json()["error"]["status"] == "UNAUTHENTICATED"
    batch = gw.post({"requests": []}, path=f"/v1beta/models/{MODEL}:batchGenerateContent")
    assert batch.status_code == 400 and batch.json()["error"]["status"] == "INVALID_ARGUMENT"
    for sneaky in (  # names Google might read as generateContent, sent as token counts
        f"/v1beta/models/{MODEL}:generateContent%3AcountTokens",
        f"/v1beta/models/{MODEL}:generateContent%2F..%2Fx:countTokens",
        f"/v1beta/models/{MODEL}%3BgenerateContent:countTokens",
    ):
        assert gw.post(_body(), path=sneaky).status_code == 400, sneaky
    assert gw.seen == [] and gw.events() == []  # nothing unrecorded reached the model


def test_token_counts_embeddings_and_model_lists_are_forwarded_unrecorded(gw: Gw) -> None:
    gw.upstream(*(httpx2.Response(200, json={"ok": n}) for n in range(4)))
    assert gw.post(_body(), path=f"/v1beta/models/{MODEL}:countTokens").json() == {"ok": 0}
    embed = "/v1beta/models/gemini-embedding-001:batchEmbedContents"
    assert gw.post({"requests": []}, path=embed).json() == {"ok": 1}
    listing = gw.client.get("/v1beta/models", headers={"x-goog-api-key": gw.key})
    assert listing.json() == {"ok": 2}
    one = gw.client.get(f"/v1beta/models/{MODEL}", headers={"x-goog-api-key": gw.key})
    assert one.json() == {"ok": 3}
    assert {str(s.url.host) for s in gw.seen} == {"generativelanguage.googleapis.com"}
    assert all(s.headers["x-goog-api-key"] == REAL for s in gw.seen)
    assert gw.events() == []


def test_org_policy_applies_to_gemini(gwp: Gw) -> None:
    first, _ = _replies()
    other = gwp.post(_body(), path="/v1beta/models/gemini-2.5-flash:generateContent")
    assert other.status_code == 403
    assert other.json()["error"]["message"] == (
        "model gemini-2.5-flash is not allowed. The org's policy allows gemini-2.5-pro"
    )
    capped = gwp.post(_body(generationConfig={"maxOutputTokens": 5000}))
    assert capped.status_code == 403
    assert "generationConfig.maxOutputTokens 5000 exceeds 1000" in capped.text
    gwp.upstream(httpx2.Response(200, json=first))
    assert gwp.post(_body()).status_code == 200  # the model asks for lookup_order: relayed
    call = first["candidates"][0]["content"]["parts"][1]["functionCall"]
    result = {"functionResponse": {"id": "lookup_order__x", "name": "lookup_order"}}
    refused = gwp.post(
        _body(
            {"role": "model", "parts": [{"functionCall": {**call, "id": "lookup_order__x"}}]},
            {"role": "user", "parts": [{**result, "response": {"output": "delivered"}}]},
        )
    )
    assert refused.status_code == 403 and refused.json()["error"]["status"] == "PERMISSION_DENIED"
    assert len(gwp.seen) == 1  # neither refusal reached the model
    denials = [
        e for e in gwp.events() if e.kind is Kind.POLICY_CHECK and not e.attrs["policy.allowed"]
    ]
    assert len(denials) == 4  # model, cap, the call, its result


def test_no_gemini_upstream_is_a_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_text("ledgers: runs\nupstreams: {}\n")
    key = add_principal(path, "alice@corp")
    sessions = Sessions(load_config(path).ledgers, None, idle=900)
    with TestClient(create_app(load_config(path), sessions)) as client:
        r = client.post(GENERATE, json=_body(), headers={"x-goog-api-key": key})
    assert r.status_code == 404 and r.json()["error"]["message"] == "no gemini upstream"
