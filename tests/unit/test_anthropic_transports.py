"""Every Anthropic call style, replayed through the real SDK: mock transport or slow server."""

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import Ledger
from seatbelt.record.recorder import Recorder
from seatbelt.verify.chain import verify_file

anthropic = pytest.importorskip("anthropic")
import httpx2  # noqa: E402

from seatbelt.adapters.anthropic import AnthropicAdapter  # noqa: E402

FIXTURE = Path(__file__).parent.parent / "fixtures" / "anthropic_refund.json"
SHAPE = [
    Kind.RUN_START,
    Kind.MODEL_REQUEST,
    Kind.MODEL_RESPONSE,
    Kind.TOOL_CALL,
    Kind.TOOL_RESULT,
    Kind.MODEL_REQUEST,
    Kind.MODEL_RESPONSE,
    Kind.RUN_END,
]


def _sse(message: dict[str, Any]) -> bytes:
    def event(name: str, data: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"

    start: dict[str, Any] = {**message, "content": [], "stop_reason": None}
    out = [event("message_start", {"message": start})]
    for i, block in enumerate(message["content"]):
        if block["type"] == "text":
            out.append(
                event("content_block_start", {"index": i, "content_block": {**block, "text": ""}})
            )
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            out.append(
                event("content_block_start", {"index": i, "content_block": {**block, "input": {}}})
            )
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        out.append(event("content_block_delta", {"index": i, "delta": delta}))
        out.append(event("content_block_stop", {"index": i}))
    usage = {"output_tokens": message["usage"]["output_tokens"]}
    out.append(
        event("message_delta", {"delta": {"stop_reason": message["stop_reason"]}, "usage": usage})
    )
    out.append(event("message_stop", {}))
    return "".join(out).encode()


def _transport() -> httpx2.MockTransport:
    replies: Iterator[dict[str, Any]] = iter(json.loads(FIXTURE.read_text()))

    def handler(request: httpx2.Request) -> httpx2.Response:
        reply = next(replies)
        if json.loads(request.content).get("stream"):
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=_sse(reply)
            )
        return httpx2.Response(200, json=reply)

    return httpx2.MockTransport(handler)


def _tool_result(first: Any) -> list[dict[str, Any]]:
    tool_use = next(b for b in first.content if b.type == "tool_use")
    return [
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": "ok"}],
        },
    ]


def _sync(rec: Recorder, style: str, beta: bool) -> None:
    client = anthropic.Anthropic(
        api_key="test", http_client=anthropic.DefaultHttpxClient(transport=_transport())
    )
    messages = AnthropicAdapter(rec).messages(client.beta if beta else client)
    history: list[dict[str, Any]] = [{"role": "user", "content": "Refund order 1001"}]
    for _ in range(2):
        kwargs: dict[str, Any] = {"model": "claude-opus-5", "max_tokens": 256, "messages": history}
        if style == "stream":
            with messages.stream(**kwargs) as stream:
                response = stream.get_final_message()
        else:
            response = messages.create(**kwargs)
        if response.stop_reason == "tool_use":
            history = [*history, *_tool_result(response)]


async def _async(rec: Recorder, style: str, beta: bool) -> None:
    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=_transport())
    )
    messages = AnthropicAdapter(rec).async_messages(client.beta if beta else client)
    history: list[dict[str, Any]] = [{"role": "user", "content": "Refund order 1001"}]
    for _ in range(2):
        kwargs: dict[str, Any] = {"model": "claude-opus-5", "max_tokens": 256, "messages": history}
        if style == "stream":
            async with messages.stream(**kwargs) as stream:
                response = await stream.get_final_message()
        else:
            response = await messages.create(**kwargs)
        if response.stop_reason == "tool_use":
            history = [*history, *_tool_result(response)]


@pytest.mark.parametrize("beta", [False, True], ids=["messages", "beta"])
@pytest.mark.parametrize("style", ["create", "stream"])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_every_call_style_records_the_same_ledger(
    tmp_path: Path, mode: str, style: str, beta: bool
) -> None:
    with Recorder.start(tmp_path, agent_id="refund-bot", run_id="r") as rec:
        if mode == "sync":
            _sync(rec, style, beta)
        else:
            asyncio.run(_async(rec, style, beta))

    path = tmp_path / "r.jsonl"
    events: list[Event] = list(Ledger(path, "r").read())
    assert [e.kind for e in events] == SHAPE
    assert verify_file(path).complete
    request, response, call, result = events[1:5]
    assert response.parent_id == request.id
    assert response.actor.version == "claude-sonnet-4-5-20250929"
    assert response.attrs["gen_ai.usage.output_tokens"] == 31
    assert call.attrs["gen_ai.tool.call.arguments"] == {"order_id": 1001}
    assert call.parent_id == response.id
    assert result.parent_id == call.id


def test_create_with_stream_true_points_to_stream(tmp_path: Path) -> None:
    client = anthropic.Anthropic(
        api_key="test", http_client=anthropic.DefaultHttpxClient(transport=_transport())
    )
    with Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec:
        messages = AnthropicAdapter(rec).messages(client)
        with pytest.raises(TypeError, match=r"\.stream\(\)"):
            messages.create(model="m", max_tokens=1, messages=[], stream=True)


@pytest.fixture
def slow_server() -> Iterator[str]:
    """Streams the tool_use reply one SSE event every 0.5s over a real socket."""
    events = [e for e in _sse(json.loads(FIXTURE.read_text())[0]).split(b"\n\n") if e]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            try:
                for event in events:
                    self.wfile.write(event + b"\n\n")
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.parametrize("close", [False, True], ids=["break", "break-and-close"])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_abandoned_stream_exits_like_the_sdk_and_records_partial(
    tmp_path: Path, slow_server: str, mode: str, close: bool
) -> None:
    kwargs: dict[str, Any] = {
        "model": "m",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "hi"}],
    }

    async def run_async(rec: Recorder) -> None:
        client = anthropic.AsyncAnthropic(api_key="test", base_url=slow_server, max_retries=0)
        async with AnthropicAdapter(rec).async_messages(client).stream(**kwargs) as stream:
            async for _ in stream:
                break
            if close:
                await stream.close()

    started = time.monotonic()
    with Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec:
        if mode == "sync":
            client = anthropic.Anthropic(api_key="test", base_url=slow_server, max_retries=0)
            with AnthropicAdapter(rec).messages(client).stream(**kwargs) as stream:
                for _ in stream:
                    break
                if close:
                    stream.close()
        else:
            asyncio.run(run_async(rec))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"wrapper waited for the stream to finish ({elapsed:.1f}s)"
    events = list(Ledger(tmp_path / "r.jsonl", "r").read())
    assert [e.kind for e in events] == [
        Kind.RUN_START,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.RUN_END,
    ]
    assert events[2].attrs["gen_ai.response"]["stop_reason"] is None
    assert events[-1].attrs["run.ok"] is True


def test_stream_exited_before_any_event_records_request_only(
    tmp_path: Path, slow_server: str
) -> None:
    client = anthropic.Anthropic(api_key="test", base_url=slow_server, max_retries=0)
    with (
        Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec,
        AnthropicAdapter(rec)
        .messages(client)
        .stream(model="m", max_tokens=5, messages=[{"role": "user", "content": "hi"}]),
    ):
        pass
    kinds = [e.kind for e in Ledger(tmp_path / "r.jsonl", "r").read()]
    assert kinds == [Kind.RUN_START, Kind.MODEL_REQUEST, Kind.RUN_END]
