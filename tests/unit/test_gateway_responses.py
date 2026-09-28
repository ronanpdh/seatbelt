"""The gateway serving the OpenAI Responses API (`POST /v1/responses`), as Codex and the
OpenAI Agents SDK call it."""

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

FIXTURE = Path(__file__).parent.parent / "fixtures" / "openai_responses_refund.json"
REAL = "sk-REAL00000000000000000000000"
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

    def post(self, body: dict[str, Any], path: str = "/v1/responses") -> httpx2.Response:
        return self.client.post(path, json=body, headers={"authorization": f"Bearer {self.key}"})

    def events(self) -> list[Event]:
        return [e for p in sorted(self.ledgers.glob("*.jsonl")) for e in read_events(p)]


def _gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str, idle: int = 900
) -> Iterator[Gw]:
    monkeypatch.setenv("OPENAI_API_KEY", REAL)
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "ledgers: runs\nupstreams:\n"
        "  openai: {url: https://api.openai.com, key_env: OPENAI_API_KEY}\n" + extra
    )
    key = add_principal(path, "alice@corp")
    cfg = load_config(path)
    sessions = Sessions(cfg.ledgers, Signer.generate(), idle=idle)
    app = create_app(cfg, sessions)
    with TestClient(app) as client:
        yield Gw(client, app, key, cfg.ledgers, sessions)
    assert sessions.close_all(timeout=1) == 0


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gw]:
    yield from _gateway(tmp_path, monkeypatch, "")


@pytest.fixture
def gwp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gw]:
    yield from _gateway(tmp_path, monkeypatch, POLICY)


POLICY = (
    "policy:\n  models: [gpt-5]\n  max_output_tokens: 1000\n"
    "  tools_denied: [lookup_order, local_shell, mcp__github__create_issue]\n"
)


def _body(*items: dict[str, Any], **extra: Any) -> dict[str, Any]:  # request JSON
    return {
        "model": "gpt-5",
        "input": [{"role": "user", "content": "refund 1001"}, *items],
        "store": False,
        **extra,
    }


def test_responses_is_routed_to_openai_and_recorded(gw: Gw) -> None:
    first, _ = _replies()
    gw.upstream(httpx2.Response(200, json=first, headers={"x-request-id": "req_1"}))
    r = gw.post(_body())
    assert r.status_code == 200 and r.json() == first and r.headers["x-request-id"] == "req_1"
    (sent,) = gw.seen
    assert sent.url == "https://api.openai.com/v1/responses"
    assert sent.headers["authorization"] == f"Bearer {REAL}"
    kinds = [e.kind for e in gw.events()]
    assert kinds == [Kind.RUN_START, Kind.MODEL_REQUEST, Kind.MODEL_RESPONSE, Kind.TOOL_CALL]


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _stream_of(response: dict[str, Any]) -> list[dict[str, Any]]:  # SSE JSON
    created: dict[str, Any] = {**response, "status": "in_progress", "output": [], "usage": None}
    events: list[dict[str, Any]] = [{"type": "response.created", "response": created}]
    for i, item in enumerate(response["output"]):
        started = {**item, "status": "in_progress"}
        if "arguments" in item:
            started["arguments"] = ""
        events.append({"type": "response.output_item.added", "output_index": i, "item": started})
        events.append({"type": "response.output_item.done", "output_index": i, "item": item})
    events.append({"type": "response.completed", "response": response})
    return [{**e, "sequence_number": n} for n, e in enumerate(events)]


def test_a_streamed_response_is_relayed_as_sent_and_recorded_whole(gw: Gw) -> None:
    first, _ = _replies()
    raw = _sse(_stream_of(first))
    gw.upstream(httpx2.Response(200, headers=SSE_HEADERS, content=raw))
    r = gw.post(_body(stream=True))
    assert r.status_code == 200 and r.content == raw
    events = gw.events()
    (response,) = [e for e in events if e.kind is Kind.MODEL_RESPONSE]
    assert response.attrs["gen_ai.response"] == first and response.attrs["error"] is None
    assert response.attrs["gen_ai.usage.output_tokens"] == 31
    assert [e.attrs["gen_ai.tool.name"] for e in events if e.kind is Kind.TOOL_CALL] == [
        "lookup_order"
    ]


def test_a_stream_that_ends_before_completing_is_recorded_as_such(gw: Gw) -> None:
    first, _ = _replies()
    # created, the reasoning item, then the function call only started: the upstream closes
    raw = _sse(_stream_of(first)[:4])
    gw.upstream(httpx2.Response(200, headers=SSE_HEADERS, content=raw))
    assert gw.post(_body(stream=True)).content == raw
    events = gw.events()
    (response,) = [e for e in events if e.kind is Kind.MODEL_RESPONSE]
    assert response.attrs["error"] == "response did not complete (status in_progress)"
    assert Kind.TOOL_CALL not in [e.kind for e in events]


def test_chat_completions_and_responses_share_a_session_without_mixing(gw: Gw) -> None:
    """Both go to the openai upstream; each keeps its own format and open calls."""
    first, second = _replies()
    chat = {
        "id": "c1",
        "model": "gpt-5",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"content": "hi"}}],
    }
    gw.upstream(
        httpx2.Response(200, json=chat),
        httpx2.Response(200, json=first),
        httpx2.Response(200, json=second),
    )
    gw.post({"model": "gpt-5", "messages": []}, path="/v1/chat/completions")
    gw.post(_body())
    output = {"type": "function_call_output", "call_id": "call_01", "output": "delivered"}
    gw.post(_body(first["output"][1], output))
    events = gw.events()
    (call,) = [e for e in events if e.kind is Kind.TOOL_CALL]
    (result,) = [e for e in events if e.kind is Kind.TOOL_RESULT]
    assert result.parent_id == call.id and result.attrs["gen_ai.tool.call.result"] == "delivered"
    assert len({e.run_id for e in events}) == 1  # one session


def test_org_policy_applies_to_responses(gwp: Gw) -> None:
    first, _ = _replies()
    assert gwp.post(_body(model="gpt-4o")).status_code == 403
    too_long = gwp.post(_body(max_output_tokens=4000))
    assert too_long.status_code == 403
    assert too_long.json()["error"]["message"] == "max_output_tokens 4000 exceeds 1000"
    gwp.upstream(httpx2.Response(200, json=first))
    assert gwp.post(_body()).status_code == 200  # the model asks for lookup_order: denied
    output = {"type": "function_call_output", "call_id": "call_01", "output": "delivered"}
    refused = gwp.post(_body(first["output"][1], output))
    assert refused.status_code == 403 and "lookup_order" in refused.json()["error"]["message"]
    assert len(gwp.seen) == 1  # nothing refused reached the upstream
    (call,) = [e for e in gwp.events() if e.kind is Kind.TOOL_CALL]
    checks = [e for e in gwp.events() if e.kind is Kind.POLICY_CHECK and e.parent_id == call.id]
    assert [c.attrs["policy.allowed"] for c in checks] == [False]


def test_the_agents_sdk_local_shell_output_is_refused_when_local_shell_is_denied(gwp: Gw) -> None:
    call = {"type": "local_shell_call", "call_id": "s1", "action": {"command": ["ls"]}}
    gwp.upstream(httpx2.Response(200, json={"status": "completed", "output": [call]}))
    assert gwp.post(_body()).status_code == 200
    output = {"type": "local_shell_call_output", "call_id": "s1", "output": "a.txt"}
    assert gwp.post(_body(call, output)).status_code == 403
    assert len(gwp.seen) == 1


def test_a_denied_mcp_tool_is_not_mistaken_for_a_bare_name(gwp: Gw) -> None:
    call = {
        "type": "function_call",
        "call_id": "m1",
        "namespace": "mcp__github",
        "name": "create_issue",
        "arguments": "{}",
    }
    gwp.upstream(httpx2.Response(200, json={"status": "completed", "output": [call]}))
    assert gwp.post(_body()).status_code == 200
    output = {"type": "function_call_output", "call_id": "m1", "output": "done"}
    refused = gwp.post(_body(call, output))
    assert refused.status_code == 403
    assert "mcp__github__create_issue" in refused.json()["error"]["message"]


def test_a_denial_outlives_the_session_for_a_chained_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With previous_response_id the client sends the tool's output without the call; the
    session that saw the call may have closed on idle meanwhile."""
    first, _ = _replies()
    gen = _gateway(tmp_path, monkeypatch, POLICY, idle=0)
    gw = next(gen)
    gw.upstream(httpx2.Response(200, json=first))
    assert gw.post(_body()).status_code == 200  # lookup_order asked for: denied
    assert gw.sessions.sweep() == 1  # the session closes
    output = {"type": "function_call_output", "call_id": "call_01", "output": "delivered"}
    chained = {"model": "gpt-5", "previous_response_id": "resp_01", "input": [output]}
    assert gw.post(chained).status_code == 403
    assert len(gw.seen) == 1
    next(gen, None)


def test_background_responses_are_refused_before_the_upstream(gw: Gw) -> None:
    r = gw.post(_body(background=True))
    assert r.status_code == 400 and "background" in r.json()["error"]["message"]
    assert gw.seen == [] and not list(gw.ledgers.glob("*.jsonl"))


def test_a_malformed_stream_still_releases_its_session(gw: Gw) -> None:
    raw = b'data: {"type": ["x"]}\n\ndata: {"type": "response.completed", "response": 5}\n\n'
    gw.upstream(httpx2.Response(200, headers=SSE_HEADERS, content=raw))
    assert gw.post(_body(stream=True)).content == raw
    (response,) = [e for e in gw.events() if e.kind is Kind.MODEL_RESPONSE]
    assert response.attrs["error"] is not None  # the fixture checks the session was released


def test_a_deferred_tool_keeps_its_name_for_tools_denied(gwp: Gw) -> None:
    """The API gives a deferred top-level tool a namespace equal to its name."""
    first, _ = _replies()
    call = {**first["output"][1], "namespace": "lookup_order"}
    gwp.upstream(httpx2.Response(200, json={**first, "output": [call]}))
    assert gwp.post(_body()).status_code == 200
    output = {"type": "function_call_output", "call_id": "call_01", "output": "delivered"}
    assert gwp.post(_body(call, output)).status_code == 403
    chained = {"model": "gpt-5", "previous_response_id": "resp_01", "input": [output]}
    assert gwp.post(chained).status_code == 403
