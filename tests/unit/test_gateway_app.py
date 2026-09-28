import contextlib
import json
from collections.abc import AsyncIterator, Callable, Iterator, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tests.helpers import sse

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer
from seatbelt.gateway.app import create_app
from seatbelt.gateway.config import add_principal, key_hash, load_config
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


POLICY = (
    "policy:\n  models: [claude-sonnet-5, gpt-5]\n  tools_denied: [run_shell]\n"
    "  max_output_tokens: 1000\n"
)


def _gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str) -> Iterator[Gateway]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REAL0000000000000000000000")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-REAL00000000000000000000000")
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text(
        "signing_key: keys/seatbelt.key\nledgers: runs\n"
        "upstreams:\n  anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}\n"
        "  openai: {url: https://api.openai.com, key_env: OPENAI_API_KEY}\n" + extra
    )
    key = add_principal(cfg_path, "alice@corp")
    cfg = load_config(cfg_path)
    seen: list[httpx2.Request] = []
    sessions = Sessions(cfg.ledgers, Signer.generate(), idle=cfg.session_idle)
    app = create_app(cfg, sessions, transport=_upstream(seen))
    with TestClient(app) as client:
        yield Gateway(client, app, key, cfg.ledgers, seen)
    assert sessions.close_all(timeout=1) == 0  # every request released its session


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gateway]:
    yield from _gateway(tmp_path, monkeypatch, "")


@pytest.fixture
def gwp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gateway]:
    """A gateway with an org policy."""
    yield from _gateway(tmp_path, monkeypatch, POLICY)


def test_unknown_key_is_401_and_nothing_is_written(gw: Gateway) -> None:
    r = gw.client.post(
        "/v1/messages", json={"model": "m", "messages": []}, headers={"x-api-key": "sbk_nope"}
    )
    assert r.status_code == 401 and not list(gw.ledgers.glob("*.jsonl")) and gw.seen == []


def test_either_credential_header_authenticates(gw: Gateway) -> None:
    """Claude Code may send x-api-key (e.g. from apiKeyHelper) and Authorization together."""
    r = gw.client.post(
        "/v1/messages",
        json={"model": "m", "messages": []},
        headers={"x-api-key": "sk-ant-console-key", "authorization": f"Bearer {gw.key}"},
    )
    assert r.status_code == 200
    assert "sk-ant-console-key" not in str(gw.seen[0].headers)  # never forwarded


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
    assert start.attrs["principal.key_id"] == key_hash(gw.key)[:12]


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
    assert (
        gw.client.post("/seatbelt/runs/job-1/end", headers={"x-api-key": gw.key}).status_code == 404
    )
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


# -- streaming ---------------------------------------------------------------

SSE_HEADERS = {"content-type": "text/event-stream", "request-id": "req_s"}


def _first() -> dict[str, Any]:
    return json.loads(ANTHROPIC.read_text())[0]


def _response(events: list[Event]) -> Event:
    (resp,) = [e for e in events if e.kind == Kind.MODEL_RESPONSE]
    return resp


def test_anthropic_stream_is_relayed_byte_for_byte_and_recorded_once(gw: Gateway) -> None:
    gw.upstream(lambda _: httpx2.Response(200, headers=SSE_HEADERS, content=sse(_first())))
    with gw.client.stream(
        "POST",
        "/v1/messages",
        json={"model": "m", "messages": [], "stream": True},
        headers={"x-api-key": gw.key},
    ) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["request-id"] == "req_s"
        raw = b"".join(r.iter_bytes())
    assert raw == sse(_first())
    events = gw.events()
    resp = _response(events)
    assert resp.attrs["gen_ai.response"]["stop_reason"] == "tool_use"
    assert resp.attrs["gen_ai.usage.output_tokens"] == 31 and resp.attrs["error"] is None
    assert events[-1].kind == Kind.TOOL_CALL


def test_openai_chat_stream_is_reassembled(gw: Gateway) -> None:
    chunks = [
        {"id": "c1", "model": "gpt-5", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {"content": "Hel"}}]},
        {
            "id": "c1",
            "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}],
        },
    ]
    body = "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks) + "data: [DONE]\r\n\r\n"
    gw.upstream(lambda _: httpx2.Response(200, headers=SSE_HEADERS, content=body.encode()))
    r = gw.client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {gw.key}"},
    )
    assert r.status_code == 200 and r.content == body.encode()
    message = _response(gw.events()).attrs["gen_ai.response"]["choices"][0]["message"]
    assert message["content"] == "Hello"


def test_upstream_that_breaks_mid_stream_is_recorded_as_partial(gw: Gateway) -> None:
    blocks = sse(_first()).split(b"\n\n")

    async def broken() -> AsyncIterator[bytes]:
        for block in blocks[:3]:
            yield block + b"\n\n"
        raise httpx2.ReadError("connection reset")

    gw.upstream(lambda _: httpx2.Response(200, headers=SSE_HEADERS, content=broken()))
    with pytest.raises(httpx2.ReadError):
        gw.client.post(
            "/v1/messages",
            json={"model": "m", "messages": [], "stream": True},
            headers={"x-api-key": gw.key},
        )
    resp = _response(gw.events())
    assert resp.attrs["gen_ai.response"]["stop_reason"] is None
    assert resp.attrs["error"].startswith("upstream stream broke: ReadError")
    assert Kind.TOOL_CALL not in [e.kind for e in gw.events()]  # inputs may be truncated


def test_stream_request_that_upstream_refuses_is_relayed_and_recorded(gw: Gateway) -> None:
    gw.upstream(lambda _: httpx2.Response(429, json={"error": "slow down"}))
    r = gw.client.post(
        "/v1/messages",
        json={"model": "m", "messages": [], "stream": True},
        headers={"x-api-key": gw.key},
    )
    assert r.status_code == 429 and r.json() == {"error": "slow down"}
    assert _response(gw.events()).attrs["error"].startswith("429")


def test_run_end_header_on_a_stream_closes_after_the_stream(gw: Gateway) -> None:
    gw.upstream(lambda _: httpx2.Response(200, headers=SSE_HEADERS, content=sse(_first())))
    headers = {"x-api-key": gw.key, "X-Seatbelt-Run": "s", "X-Seatbelt-Run-End": "true"}
    gw.client.post(
        "/v1/messages", json={"model": "m", "messages": [], "stream": True}, headers=headers
    )
    (ledger,) = gw.ledgers.glob("*.jsonl")
    kinds = [e.kind for e in read_events(ledger)]
    assert kinds[-2:] == [Kind.TOOL_CALL, Kind.RUN_END] and verify_file(ledger).complete


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_client_that_disconnects_mid_stream_is_recorded_and_released(
    gw: Gateway, spec_version: str
) -> None:
    """Raw ASGI: TestClient buffers the whole response, so it never disconnects early.
    2.3 reports the disconnect through receive(); 2.4 through send() raising OSError."""
    blocks = sse(_first()).split(b"\n\n")

    async def slow() -> AsyncIterator[bytes]:
        for block in blocks[:3]:
            yield block + b"\n\n"
        await anyio.sleep(30)  # the client leaves long before this
        yield b""

    gw.upstream(lambda _: httpx2.Response(200, headers=SSE_HEADERS, content=slow()))
    request = json.dumps({"model": "m", "messages": [], "stream": True}).encode()

    async def drive() -> None:
        pending: list[dict[str, Any]] = [{"type": "http.request", "body": request}]
        first_chunk = anyio.Event()

        async def receive() -> dict[str, Any]:
            if pending:
                return pending.pop()
            await first_chunk.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                if first_chunk.is_set() and spec_version == "2.4":
                    raise OSError("client went away")
                first_chunk.set()

        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/messages",
            "raw_path": b"/v1/messages",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"x-api-key", gw.key.encode()), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 5000),
            "server": ("testserver", 80),
        }
        with anyio.fail_after(10), contextlib.suppress(Exception):
            await gw.app(scope, receive, send)

    anyio.run(drive)
    resp = _response(gw.events())
    assert resp.attrs["gen_ai.response"]["stop_reason"] is None
    assert resp.attrs["error"] == "stream ended early"  # teardown checks the release


# -- org policy ----------------------------------------------------------------


def test_no_policy_records_no_checks(gw: Gateway) -> None:
    gw.client.post(
        "/v1/messages", json={"model": "m", "messages": []}, headers={"x-api-key": gw.key}
    )
    assert Kind.POLICY_CHECK not in [e.kind for e in gw.events()]


def test_allowed_request_records_a_check_per_rule(gwp: Gateway) -> None:
    r = gwp.client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-5", "max_tokens": 10, "messages": []},
        headers={"x-api-key": gwp.key},
    )
    assert r.status_code == 200
    events = gwp.events()
    request = next(e for e in events if e.kind == Kind.MODEL_REQUEST)
    checks = [e for e in events if e.kind == Kind.POLICY_CHECK and e.parent_id == request.id]
    assert [(c.actor.id, c.attrs["policy.allowed"]) for c in checks] == [
        ("models", True),
        ("max_output_tokens", True),
    ]


def test_disallowed_model_is_403_and_recorded(gwp: Gateway) -> None:
    r = gwp.client.post(
        "/v1/messages",
        json={"model": "claude-opus-5", "max_tokens": 10, "messages": []},
        headers={"x-api-key": gwp.key},
    )
    assert r.status_code == 403 and gwp.seen == []
    assert r.json()["error"] == {
        "type": "permission_error",
        "message": "model claude-opus-5 is not allowed",
    }
    events = gwp.events()
    denied = [e for e in events if e.kind == Kind.POLICY_CHECK and not e.attrs["policy.allowed"]]
    assert [d.actor.id for d in denied] == ["models"]
    assert denied[0].parent_id == events[1].id and events[1].kind == Kind.MODEL_REQUEST
    assert Kind.MODEL_RESPONSE not in [e.kind for e in events]  # no model ever answered


def test_output_token_cap_applies_to_openai_too(gwp: Gateway) -> None:
    r = gwp.client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5", "max_completion_tokens": 5000, "messages": []},
        headers={"authorization": f"Bearer {gwp.key}"},
    )
    assert r.status_code == 403 and "exceeds 1000" in r.json()["error"]["message"]


def _shell_reply() -> dict[str, Any]:
    shell = {**_first()}
    shell["content"] = [
        {"type": "tool_use", "id": "toolu_09", "name": "run_shell", "input": {"command": "ls"}}
    ]
    return shell


def _followup(body: dict[str, Any], shell: dict[str, Any]) -> dict[str, Any]:
    result = {"type": "tool_result", "tool_use_id": "toolu_09", "content": "file.txt"}
    return {
        **body,
        "messages": [
            *body["messages"],
            {"role": "assistant", "content": shell["content"]},
            {"role": "user", "content": [result]},
        ],
    }


def test_denied_tool_is_recorded_and_its_result_refused(gwp: Gateway) -> None:
    shell = _shell_reply()
    gwp.upstream(lambda _: httpx2.Response(200, json=shell))
    body: dict[str, Any] = {
        "model": "claude-sonnet-5",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "ls"}],
    }
    r = gwp.client.post("/v1/messages", json=body, headers={"x-api-key": gwp.key})
    assert r.status_code == 200  # relayed: the gateway cannot stop a local tool
    r = gwp.client.post("/v1/messages", json=_followup(body, shell), headers={"x-api-key": gwp.key})
    assert r.status_code == 403 and "run_shell" in r.json()["error"]["message"]
    assert len(gwp.seen) == 1  # the result never reached the model
    events = gwp.events()
    denied = [e for e in events if e.kind == Kind.POLICY_CHECK and not e.attrs["policy.allowed"]]
    assert [d.actor.id for d in denied] == ["denylist", "denylist"]
    (call,) = [e for e in events if e.kind == Kind.TOOL_CALL]
    assert denied[0].parent_id == call.id
    assert Kind.TOOL_RESULT in [e.kind for e in events]  # what came back is still evidence


def test_denied_tool_result_is_refused_in_a_new_session_too(gwp: Gateway) -> None:
    """The idle window closed the session that denied the call; the history still names it."""
    body: dict[str, Any] = {"model": "claude-sonnet-5", "max_tokens": 10, "messages": []}
    r = gwp.client.post(
        "/v1/messages", json=_followup(body, _shell_reply()), headers={"x-api-key": gwp.key}
    )
    assert r.status_code == 403 and gwp.seen == []


def test_denied_tool_in_a_stream_is_recorded(gwp: Gateway) -> None:
    gwp.upstream(lambda _: httpx2.Response(200, headers=SSE_HEADERS, content=sse(_shell_reply())))
    gwp.client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-5", "max_tokens": 10, "messages": [], "stream": True},
        headers={"x-api-key": gwp.key},
    )
    last = gwp.events()[-1]
    assert last.kind == Kind.POLICY_CHECK and last.attrs["policy.allowed"] is False


# -- probes --------------------------------------------------------------------


def test_hello_probe_is_answered_locally(gw: Gateway) -> None:
    assert gw.client.head("/api/hello").status_code == 200 and gw.seen == []
    assert gw.client.get("/api/hello").status_code == 200


def test_count_tokens_and_models_are_forwarded_not_recorded(gw: Gateway) -> None:
    gw.upstream(
        lambda r: httpx2.Response(
            200, json={"input_tokens": 5} if "count" in r.url.path else {"data": []}
        )
    )
    r = gw.client.post(
        "/v1/messages/count_tokens",
        json={"model": "m", "messages": []},
        headers={"x-api-key": gw.key},
    )
    assert r.json() == {"input_tokens": 5}
    assert gw.client.get("/v1/models?limit=5", headers={"x-api-key": gw.key}).status_code == 200
    assert (
        gw.client.get("/v1/models/gpt-5", headers={"authorization": f"Bearer {gw.key}"}).status_code
        == 200
    )
    assert [(r.url.host, r.url.path, r.url.query) for r in gw.seen] == [
        ("api.anthropic.com", "/v1/messages/count_tokens", b""),
        ("api.anthropic.com", "/v1/models", b"limit=5"),
        ("api.openai.com", "/v1/models/gpt-5", b""),
    ]
    assert gw.seen[0].headers["x-api-key"].startswith("sk-ant-REAL")
    assert gw.seen[2].headers["authorization"] == "Bearer sk-REAL00000000000000000000000"
    assert not list(gw.ledgers.glob("*.jsonl"))
    assert gw.client.get("/v1/models").status_code == 401
