"""Anthropic Messages wire format -> recorder events, on plain dicts (no SDK)."""

import json
from pathlib import Path
from typing import Any

from seatbelt.gateway.formats.anthropic import AnthropicFormat, assemble_sse
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder

FIXTURE = Path(__file__).parent.parent / "fixtures" / "anthropic_refund.json"


def _replies() -> list[dict[str, Any]]:  # fixture JSON
    return json.loads(FIXTURE.read_text())


def test_request_response_tool_call_and_result_are_recorded(tmp_path: Path) -> None:
    first, second = _replies()
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = AnthropicFormat(rec)
        body: dict[str, Any] = {  # request JSON
            "model": "claude-sonnet-5",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }
        call = fmt.begin(body)
        usage = {**first["usage"], "cache_read_input_tokens": 7, "cache_creation": {"ephemeral": 3}}
        calls = fmt.finish(call, {**first, "usage": usage})
        assert [c.attrs["gen_ai.tool.name"] for c in calls] == ["lookup_order"]
        history = body["messages"] + [
            {"role": "assistant", "content": first["content"]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "ok"}],
            },
        ]
        call2 = fmt.begin({**body, "messages": history})
        assert fmt.finish(call2, second) == []
    events = list(read_events(tmp_path / "s.jsonl"))
    assert [e.kind for e in events] == [
        Kind.RUN_START,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.RUN_END,
    ]
    resp = events[2]
    assert resp.attrs["gen_ai.response.model"] == first["model"]
    assert resp.attrs["gen_ai.usage.output_tokens"] == 31
    assert resp.attrs["gen_ai.usage.cache_read_input_tokens"] == 7
    assert "gen_ai.usage.cache_creation" not in resp.attrs
    assert events[4].parent_id == events[3].id


def test_error_response_is_recorded_with_no_tool_calls(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = AnthropicFormat(rec)
        call = fmt.begin({"model": "m", "messages": []})
        assert fmt.finish(call, None, error="529: overloaded") == []
    resp = next(e for e in read_events(tmp_path / "s.jsonl") if e.kind == Kind.MODEL_RESPONSE)
    assert resp.attrs["error"] == "529: overloaded"


def test_assemble_sse_rebuilds_the_message() -> None:
    first, _ = _replies()
    tool_use: dict[str, Any] = {  # SSE JSON
        "type": "tool_use",
        "id": "toolu_01",
        "name": "lookup_order",
        "input": {},
    }
    events: list[dict[str, Any]] = [  # SSE JSON
        {"type": "message_start", "message": {**first, "content": [], "stop_reason": None}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Let me look "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "that order up."},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": tool_use},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"order_id": '},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "1001}"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 31},
        },
        {"type": "message_stop"},
    ]
    assert assemble_sse(events) == first


def test_assemble_sse_of_an_abandoned_stream_has_no_stop_reason() -> None:
    first, _ = _replies()
    events: list[dict[str, Any]] = [  # SSE JSON
        {"type": "message_start", "message": {**first, "content": [], "stop_reason": None}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let"}},
    ]
    out = assemble_sse(events)
    assert out["stop_reason"] is None
    assert out["content"] == [{"type": "text", "text": "Let"}]


def test_assemble_sse_tolerates_a_malformed_stream() -> None:
    events: list[dict[str, Any]] = [  # SSE JSON
        {"type": "message_start"},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "x"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"a": '},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    out = assemble_sse(events)
    assert out["stop_reason"] is None
    assert out["content"] == [{"type": "tool_use", "input": {}}, {"text": "x"}]


def test_finish_tolerates_malformed_provider_json(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = AnthropicFormat(rec)
        call = fmt.begin({"model": "m", "messages": []})
        bad: dict[str, Any] = {
            "content": "oops",
            "usage": 5,
            "model": 7,
            "stop_reason": "end_turn",
        }  # JSON
        assert fmt.finish(call, bad) == []
        call = fmt.begin({"model": "m", "messages": []})
        content: list[Any] = ["str", {"type": "tool_use", "name": "no_id", "input": {}}]  # JSON
        assert fmt.finish(call, {**bad, "content": content}) == []
    kinds = [e.kind for e in read_events(tmp_path / "s.jsonl")]
    assert kinds.count(Kind.MODEL_RESPONSE) == 2
    assert Kind.TOOL_CALL not in kinds
