"""Pass-through upstreams (no `key_env`): the client's own credentials go on, it names its
seatbelt key in `x-seatbelt-key`, and sign-ins with their own backends are routed to them:
Codex signed in with ChatGPT, Gemini CLI signed in with Google (Code Assist)."""

import gzip
import json
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from seatbelt.attest.sign import Signer
from seatbelt.gateway.app import create_app
from seatbelt.gateway.config import GatewayConfig, PolicyConfig, Principal, Upstream, key_hash
from seatbelt.gateway.sessions import Sessions
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import read_events

KEY = "sbk_run-key-for-tests"
UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "chatgpt": "https://chatgpt.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "codeassist": "https://cloudcode-pa.googleapis.com",
}
FIXTURES = Path(__file__).parent.parent / "fixtures"


@dataclass
class Gw:
    client: TestClient
    app: Starlette
    ledgers: Path
    seen: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def upstream(self, *replies: httpx2.Response) -> None:
        queue = list(replies)

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.seen.append(request)
            return queue.pop(0)

        self.app.state.transport = httpx2.MockTransport(handler)

    def events(self) -> list[Event]:
        return [e for p in sorted(self.ledgers.glob("*.jsonl")) for e in read_events(p)]


def _gateway(tmp_path: Path, policy: PolicyConfig | None = None) -> Iterator[Gw]:
    cfg = GatewayConfig(
        ledgers=tmp_path / "runs",
        upstreams={name: Upstream(url=url) for name, url in UPSTREAMS.items()},
        principals=[Principal(id="me", key_sha256=key_hash(KEY), issued=date.today())],
        policy=policy or PolicyConfig(),
    )
    sessions = Sessions(cfg.ledgers, Signer.generate(), idle=900)
    app = create_app(cfg, sessions)
    with TestClient(app) as client:
        yield Gw(client, app, cfg.ledgers)
    assert sessions.close_all(timeout=1) == 0


@pytest.fixture
def gw(tmp_path: Path) -> Iterator[Gw]:
    yield from _gateway(tmp_path)


@pytest.fixture
def capped(tmp_path: Path) -> Iterator[Gw]:
    yield from _gateway(tmp_path, PolicyConfig(max_output_tokens=100))


def _replies(name: str) -> list[dict[str, Any]]:  # fixture JSON
    return json.loads((FIXTURES / name).read_text())


MESSAGE = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}


def test_the_clients_own_credentials_go_on_and_only_seatbelts_headers_are_dropped(
    gw: Gw,
) -> None:
    gw.upstream(httpx2.Response(200, json={"type": "message", "content": []}))
    r = gw.client.post(
        "/v1/messages?beta=true",
        json=MESSAGE,
        headers={
            "authorization": "Bearer sk-ant-oat01-SUBSCRIPTION",
            "anthropic-beta": "oauth-2025-04-20",
            "x-seatbelt-key": KEY,
            "x-seatbelt-run": "claude-1",
        },
    )
    assert r.status_code == 200
    (sent,) = gw.seen
    assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert sent.headers["authorization"] == "Bearer sk-ant-oat01-SUBSCRIPTION"
    assert sent.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-seatbelt-key" not in sent.headers and "x-seatbelt-run" not in sent.headers


def test_a_seatbelt_key_is_never_passed_on_whatever_header_it_is_in(gw: Gw) -> None:
    gw.upstream(httpx2.Response(200, json={"type": "message", "content": []}))
    headers = {"x-seatbelt-key": KEY, "x-api-key": "sbk_something-else"}
    gw.client.post("/v1/messages", json=MESSAGE, headers=headers)
    (sent,) = gw.seen
    assert "x-api-key" not in sent.headers


def test_without_the_runs_key_nothing_is_forwarded(gw: Gw) -> None:
    r = gw.client.post("/v1/messages", json=MESSAGE, headers={"x-api-key": "sk-ant-real"})
    assert r.status_code == 401 and gw.seen == [] and gw.events() == []


def test_codex_signed_in_with_chatgpt_goes_to_chatgpts_backend(gw: Gw) -> None:
    first, _ = _replies("openai_responses_refund.json")
    body = {"model": "gpt-5", "input": [{"role": "user", "content": "refund 1001"}]}
    chatgpt = {"authorization": "Bearer eyJ.chatgpt.jwt", "chatgpt-account-id": "acct-1"}
    gw.upstream(
        httpx2.Response(200, json=first),
        httpx2.Response(200, json=first),
        httpx2.Response(200, json={"models": []}),
    )
    for headers in (chatgpt, {"authorization": "Bearer sk-proj-APIKEY"}):
        r = gw.client.post("/v1/responses", json=body, headers={**headers, "x-seatbelt-key": KEY})
        assert r.status_code == 200
    models = gw.client.get(
        "/v1/models?client_version=0.158.0", headers={**chatgpt, "x-seatbelt-key": KEY}
    )
    assert models.status_code == 200
    via_chatgpt, via_api, listing = gw.seen
    assert str(via_chatgpt.url) == "https://chatgpt.com/backend-api/codex/responses"
    assert via_chatgpt.headers["chatgpt-account-id"] == "acct-1"
    assert str(via_api.url) == "https://api.openai.com/v1/responses"
    assert str(listing.url) == "https://chatgpt.com/backend-api/codex/models?client_version=0.158.0"
    calls = [e for e in gw.events() if e.kind is Kind.TOOL_CALL]
    assert len(calls) == 2  # both recorded alike


def _code_assist(parts: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:  # request JSON
    return {
        "model": "gemini-2.5-pro",
        "project": "p-123",
        "user_prompt_id": "u-1",
        "request": {"contents": [{"role": "user", "parts": parts}], **extra},
    }


GOOGLE = {"authorization": "Bearer ya29.GOOGLE", "x-seatbelt-key": KEY}


def test_gemini_cli_signed_in_with_google_is_recorded_through_code_assist(gw: Gw) -> None:
    first, second = _replies("gemini_refund.json")
    chunk = {"response": first, "traceId": "t-1"}
    sse = f"data: {json.dumps(chunk)}\r\n\r\n".encode()
    gw.upstream(
        httpx2.Response(200, json={"currentTier": {"id": "free-tier"}}),  # loadCodeAssist
        httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse),
        httpx2.Response(200, json={"response": second, "traceId": "t-2"}),
    )
    assert gw.client.post("/v1internal:loadCodeAssist", json={}, headers=GOOGLE).status_code == 200
    stream = gw.client.post(
        "/v1internal:streamGenerateContent?alt=sse",
        json=_code_assist([{"text": "refund 1001"}]),
        headers=GOOGLE,
    )
    assert stream.content == sse
    result = {"functionResponse": {"name": "lookup_order", "response": {"output": "delivered"}}}
    answer = gw.client.post(
        "/v1internal:generateContent", json=_code_assist([result]), headers=GOOGLE
    )
    assert answer.json()["traceId"] == "t-2"
    urls = [str(s.url) for s in gw.seen]
    assert urls == [
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse",
        "https://cloudcode-pa.googleapis.com/v1internal:generateContent",
    ]
    assert all(s.headers["authorization"] == "Bearer ya29.GOOGLE" for s in gw.seen)
    events = gw.events()
    assert [e.kind for e in events][1:] == [
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
    ]
    request, response, call, result_event = events[1:5]
    assert request.attrs["gen_ai.request.model"] == "gemini-2.5-pro"
    assert request.attrs["gen_ai.request"]["contents"][0]["parts"] == [{"text": "refund 1001"}]
    assert response.attrs["gen_ai.response"]["traceId"] == "t-1"
    assert response.attrs["gen_ai.usage.output_tokens"] == 31
    assert result_event.parent_id == call.id


def test_code_assist_calls_that_are_not_known_are_refused(gw: Gw) -> None:
    gw.upstream(httpx2.Response(200, json={}))
    r = gw.client.post("/v1internal:generateChat", json={}, headers=GOOGLE)
    assert r.status_code == 400 and r.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert gw.seen == []


def test_the_output_cap_reaches_inside_a_code_assist_request(capped: Gw) -> None:
    body = _code_assist([{"text": "hi"}], generationConfig={"maxOutputTokens": 5000})
    r = capped.client.post("/v1internal:generateContent", json=body, headers=GOOGLE)
    assert r.status_code == 403
    assert "request.generationConfig.maxOutputTokens 5000 exceeds 100" in r.text


def test_a_compressed_body_is_recorded_and_sent_on_as_it_came(gw: Gw) -> None:
    gw.upstream(*(httpx2.Response(200, json={"type": "message", "content": []}),) * 2)
    for encoding, packed in (
        ("gzip", gzip.compress(json.dumps(MESSAGE).encode())),
        ("deflate", zlib.compress(json.dumps(MESSAGE).encode())),
    ):
        headers = {"content-encoding": encoding, "x-seatbelt-key": KEY}
        r = gw.client.post("/v1/messages", content=packed, headers=headers)
        assert r.status_code == 200, encoding
        assert gw.seen[-1].content == packed and gw.seen[-1].headers["content-encoding"] == encoding
    requests = [e for e in gw.events() if e.kind is Kind.MODEL_REQUEST]
    assert [e.attrs["gen_ai.request"]["messages"] for e in requests] == [MESSAGE["messages"]] * 2


def test_a_body_it_cannot_read_is_refused_not_passed_on(gw: Gw) -> None:
    bomb = gzip.compress(b"{" + b" " * (65 * 1024 * 1024) + b"}")
    for encoding, content in (("zstd", b"\x28\xb5\x2f\xfd"), ("gzip", bomb)):
        headers = {"content-encoding": encoding, "x-seatbelt-key": KEY}
        r = gw.client.post("/v1/messages", content=content, headers=headers)
        assert r.status_code == 415, encoding
    assert gw.seen == []


def test_the_runs_key_and_name_can_come_in_the_path_and_go_no_further(gw: Gw) -> None:
    """`seatbelt run` recording locally puts them in the base URL: a CLI's custom headers
    also go to other hosts."""
    gw.upstream(httpx2.Response(200, json={"type": "message", "content": []}))
    r = gw.client.post(
        f"/_seatbelt/{KEY}/claude-1a2b/v1/messages?beta=true",
        json=MESSAGE,
        headers={"x-api-key": "sk-ant-USERS-OWN", "x-seatbelt-run": "spoofed"},
    )
    assert r.status_code == 200
    (sent,) = gw.seen
    assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert sent.headers["x-api-key"] == "sk-ant-USERS-OWN"
    assert KEY not in str(sent.url) and KEY not in str(sent.headers)
    assert gw.events()[0].attrs["run.name"] == "claude-1a2b"  # the path's, not the header's
    wrong = gw.client.post("/_seatbelt/sbk_wrong/claude-1a2b/v1/messages", json=MESSAGE)
    assert wrong.status_code == 401 and len(gw.seen) == 1
