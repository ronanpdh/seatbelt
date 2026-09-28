import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer
from seatbelt.gateway.app import create_app
from seatbelt.gateway.config import add_principal, load_config
from seatbelt.gateway.sessions import Sessions
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import read_events
from seatbelt.verify.chain import verify_file

ANTHROPIC = Path(__file__).parent.parent / "fixtures" / "anthropic_refund.json"
CHAT = Path(__file__).parent.parent / "fixtures" / "openai_chat_refund.json"


@dataclass
class Gateway:
    client: TestClient
    app: Starlette
    key: str
    ledgers: Path
    seen: list[httpx2.Request]

    def upstream(self, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        def record(request: httpx2.Request) -> httpx2.Response:
            self.seen.append(request)
            return handler(request)

        self.app.state.transport = httpx2.MockTransport(record)

    def events(self) -> list[Event]:
        (ledger,) = self.ledgers.glob("*.jsonl")
        return list(read_events(ledger))


def _upstream(seen: list[httpx2.Request]) -> httpx2.MockTransport:
    replies: dict[str, Iterator[dict[str, Any]]] = {
        "api.anthropic.com": iter(json.loads(ANTHROPIC.read_text())),
        "api.openai.com": iter(json.loads(CHAT.read_text())),
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=next(replies[request.url.host]))

    return httpx2.MockTransport(handler)


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gateway]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REAL0000000000000000000000")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-REAL00000000000000000000000")
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text(
        "signing_key: keys/seatbelt.key\nledgers: runs\n"
        "upstreams:\n  anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}\n"
        "  openai: {url: https://api.openai.com, key_env: OPENAI_API_KEY}\n"
    )
    key = add_principal(cfg_path, "alice@corp")
    cfg = load_config(cfg_path)
    seen: list[httpx2.Request] = []
    sessions = Sessions(cfg.ledgers, Signer.generate(), idle=cfg.session_idle)
    app = create_app(cfg, sessions, transport=_upstream(seen))
    with TestClient(app) as client:
        yield Gateway(client, app, key, cfg.ledgers, seen)
    assert sessions.close_all(timeout=1) == 0  # every request released its session


def test_unknown_key_is_401_and_nothing_is_written(gw: Gateway) -> None:
    r = gw.client.post(
        "/v1/messages", json={"model": "m", "messages": []}, headers={"x-api-key": "sbk_nope"}
    )
    assert r.status_code == 401 and not list(gw.ledgers.glob("*.jsonl")) and gw.seen == []


def test_anthropic_round_trip_is_relayed_and_recorded(gw: Gateway) -> None:
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "refund 1001"}],
    }
    r = gw.client.post(
        "/v1/messages",
        json=body,
        headers={
            "x-api-key": gw.key,
            "anthropic-version": "2023-06-01",
            "X-Seatbelt-Run": "job-1",
            "accept-encoding": "br, zstd",
        },
    )
    assert r.status_code == 200 and r.json()["id"] == "msg_01"
    sent = gw.seen[0]
    assert sent.headers["x-api-key"].startswith("sk-ant-REAL") and "sbk_" not in str(sent.headers)
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert "x-seatbelt-run" not in sent.headers and "br" not in sent.headers["accept-encoding"]
    assert json.loads(sent.content) == body
    kinds = [e.kind for e in gw.events()]
    assert kinds == [Kind.RUN_START, Kind.MODEL_REQUEST, Kind.MODEL_RESPONSE, Kind.TOOL_CALL]
    start = gw.events()[0]
    assert start.attrs["principal.id"] == "alice@corp" and start.attrs["client.user_agent"]


def test_a_session_carries_tool_calls_across_requests(gw: Gateway) -> None:
    headers = {"x-api-key": gw.key}
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    gw.client.post("/v1/messages", json={"model": "m", "messages": messages}, headers=headers)
    result = {"type": "tool_result", "tool_use_id": "toolu_01", "content": "shipped"}
    messages.append({"role": "user", "content": [result]})
    gw.client.post("/v1/messages", json={"model": "m", "messages": messages}, headers=headers)
    events = gw.events()
    (call,) = [e for e in events if e.kind == Kind.TOOL_CALL]
    (answer,) = [e for e in events if e.kind == Kind.TOOL_RESULT]
    assert answer.parent_id == call.id and answer.attrs["gen_ai.tool.call.result"] == "shipped"


def test_openai_chat_is_routed_by_path(gw: Gateway) -> None:
    r = gw.client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {gw.key}"},
    )
    assert r.status_code == 200 and gw.seen[0].url.host == "api.openai.com"
    assert gw.seen[0].headers["authorization"] == "Bearer sk-REAL00000000000000000000000"
    assert gw.events()[0].attrs["principal.id"] == "alice@corp"


def test_upstream_error_is_relayed_with_its_headers_and_recorded(gw: Gateway) -> None:
    gw.upstream(
        lambda _: httpx2.Response(
            529,
            json={"type": "error", "error": {"type": "overloaded_error"}},
            headers={"retry-after": "7", "request-id": "req_1"},
        )
    )
    r = gw.client.post(
        "/v1/messages", json={"model": "m", "messages": []}, headers={"x-api-key": gw.key}
    )
    assert r.status_code == 529 and r.json()["error"]["type"] == "overloaded_error"
    assert r.headers["retry-after"] == "7" and r.headers["request-id"] == "req_1"
    resp = [e for e in gw.events() if e.kind == Kind.MODEL_RESPONSE][-1]
    assert resp.attrs["error"].startswith("529")


def test_unreachable_upstream_is_502_and_recorded(gw: Gateway) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    gw.upstream(down)
    r = gw.client.post(
        "/v1/messages", json={"model": "m", "messages": []}, headers={"x-api-key": gw.key}
    )
    assert r.status_code == 502
    resp = [e for e in gw.events() if e.kind == Kind.MODEL_RESPONSE][-1]
    assert "ConnectError" in resp.attrs["error"]


def test_a_request_that_fails_midway_still_answers_its_call_and_releases(gw: Gateway) -> None:
    def broken(_: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("bug in the gateway")

    gw.upstream(broken)
    with pytest.raises(RuntimeError):
        gw.client.post(
            "/v1/messages", json={"model": "m", "messages": []}, headers={"x-api-key": gw.key}
        )
    resp = [e for e in gw.events() if e.kind == Kind.MODEL_RESPONSE][-1]
    assert resp.attrs["error"] == "request did not complete"  # teardown checks the release


def test_missing_upstream_is_404(gw: Gateway, tmp_path: Path) -> None:
    cfg_path = tmp_path / "anthropic-only.yaml"
    cfg_path.write_text(
        "signing_key: k\nledgers: other\n"
        "upstreams:\n  anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}\n"
    )
    key = add_principal(cfg_path, "bob@corp")
    cfg = load_config(cfg_path)
    sessions = Sessions(cfg.ledgers, None, idle=60)
    with TestClient(create_app(cfg, sessions)) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5", "messages": []},
            headers={"authorization": f"Bearer {key}"},
        )
    assert r.status_code == 404 and not list(cfg.ledgers.glob("*.jsonl"))


def test_body_that_is_not_a_json_object_is_400_and_nothing_is_written(gw: Gateway) -> None:
    for content in (b"{not json", b"[1, 2]"):
        r = gw.client.post(
            "/v1/messages",
            content=content,
            headers={"x-api-key": gw.key, "content-type": "application/json"},
        )
        assert r.status_code == 400
    assert not list(gw.ledgers.glob("*.jsonl")) and gw.seen == []


def test_session_end_closes_and_signs(gw: Gateway) -> None:
    gw.client.post(
        "/v1/messages",
        json={"model": "m", "messages": []},
        headers={"x-api-key": gw.key, "X-Seatbelt-Run": "job-1"},
    )
    r = gw.client.post("/seatbelt/runs/job-1/end", headers={"x-api-key": gw.key})
    assert r.status_code == 204
    assert gw.client.post("/seatbelt/runs/job-1/end", headers={"x-api-key": gw.key}).status_code == 404
    (ledger,) = gw.ledgers.glob("*.jsonl")
    assert verify_file(ledger).complete and sidecar(ledger).exists()


def test_run_end_header_closes_after_the_response_is_recorded(gw: Gateway) -> None:
    headers = {"x-api-key": gw.key, "X-Seatbelt-Run": "job-2", "X-Seatbelt-Run-End": "true"}
    r = gw.client.post("/v1/messages", json={"model": "m", "messages": []}, headers=headers)
    assert r.status_code == 200 and "x-seatbelt-run-end" not in gw.seen[0].headers
    (ledger,) = gw.ledgers.glob("*.jsonl")
    kinds = [e.kind for e in read_events(ledger)]
    assert kinds[-2] == Kind.TOOL_CALL and kinds[-1] == Kind.RUN_END
    assert verify_file(ledger).complete and sidecar(ledger).exists()


def test_parallel_requests_share_one_session_and_one_chain(gw: Gateway) -> None:
    from concurrent.futures import ThreadPoolExecutor

    gw.upstream(lambda _: httpx2.Response(200, json=json.loads(ANTHROPIC.read_text())[1]))

    def call(i: int) -> int:
        return gw.client.post(
            "/v1/messages",
            json={"model": "m", "messages": [{"role": "user", "content": str(i)}]},
            headers={"x-api-key": gw.key, "X-Seatbelt-Run": ""},  # empty means unnamed
        ).status_code

    with ThreadPoolExecutor(8) as pool:
        assert set(pool.map(call, range(16))) == {200}
    events = gw.events()  # exactly one ledger
    assert sum(e.kind == Kind.MODEL_REQUEST for e in events) == 16
    assert sum(e.kind == Kind.MODEL_RESPONSE for e in events) == 16
    assert events[0].attrs["run.name"] is None
