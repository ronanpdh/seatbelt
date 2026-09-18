"""OpenAI Chat Completions wire format -> recorder events, on plain dicts."""

import json
from pathlib import Path
from typing import Any

from seatbelt.gateway.formats.openai_chat import OpenAIChatFormat, assemble_sse
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder

FIXTURE = Path(__file__).parent.parent / "fixtures" / "openai_chat_refund.json"


def _replies() -> list[dict[str, Any]]:  # fixture JSON
    return json.loads(FIXTURE.read_text())


def test_tool_calls_and_tool_messages_are_linked(tmp_path: Path) -> None:
    first, second = _replies()
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIChatFormat(rec)
        body: dict[str, Any] = {  # request JSON
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "refund 1001"}],
        }
        usage = {
            **first["usage"],
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 2, "x": "no"},
        }
        calls = fmt.finish(fmt.begin(body), {**first, "usage": usage})
        assert calls[0].attrs["gen_ai.tool.call.arguments"] == {"order_id": "1001"}
        history = body["messages"] + [
            first["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call_01", "content": '{"status": "delivered"}'},
        ]
        assert fmt.finish(fmt.begin({**body, "messages": history}), second) == []
    events = list(read_events(tmp_path / "s.jsonl"))
    assert [e.kind for e in events][1:-1] == [
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
    ]
    assert events[2].attrs["gen_ai.usage.input_tokens"] == 120
    assert events[2].attrs["gen_ai.usage.output_tokens"] == 31
    assert events[2].attrs["gen_ai.response.model"] == "gpt-5-2025-08-07"
    assert events[2].attrs["gen_ai.usage.input_tokens.cached_tokens"] == 5
    assert events[2].attrs["gen_ai.usage.output_tokens.reasoning_tokens"] == 2
    assert not [k for k in events[2].attrs if k.endswith(".x")]
    assert events[4].parent_id == events[3].id
    assert events[4].attrs["gen_ai.tool.call.result"] == '{"status": "delivered"}'


def test_unparseable_arguments_are_kept_raw(tmp_path: Path) -> None:
    first, _ = _replies()
    first["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIChatFormat(rec)
        calls = fmt.finish(fmt.begin({"model": "gpt-5", "messages": []}), first)
    assert calls[0].attrs["gen_ai.tool.call.arguments"] == {"_raw": "{not json"}


def test_error_and_malformed_responses_record_without_tool_calls(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIChatFormat(rec)
        assert fmt.finish(fmt.begin({"model": "m", "messages": []}), None, error="500: boom") == []
        bad: dict[str, Any] = {"choices": "oops", "usage": 5, "model": 7}  # JSON
        assert fmt.finish(fmt.begin({"model": "m", "messages": 5}), bad) == []
        odd: dict[str, Any] = {  # JSON
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"tool_calls": [3, {"function": {"name": "x"}}]},
                }
            ]
        }
        assert fmt.finish(fmt.begin({"model": "m", "messages": []}), odd) == []
    events = list(read_events(tmp_path / "s.jsonl"))
    assert [e.kind for e in events].count(Kind.MODEL_RESPONSE) == 3
    assert Kind.TOOL_CALL not in [e.kind for e in events]
    assert events[2].attrs["error"] == "500: boom"


def test_assemble_sse_rebuilds_a_tool_call_completion() -> None:
    first, _ = _replies()
    chunks: list[dict[str, Any]] = [  # SSE JSON
        {
            "id": "chatcmpl-01",
            "object": "chat.completion.chunk",
            "created": 1789400000,
            "model": "gpt-5-2025-08-07",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_01",
                                "type": "function",
                                "function": {"name": "lookup_order", "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"order_id": '}}]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"1001"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {"prompt_tokens": 120, "completion_tokens": 31, "total_tokens": 151},
        },
    ]
    assert assemble_sse(chunks) == first


def test_assemble_sse_of_text_and_of_an_abandoned_stream() -> None:
    text = assemble_sse(
        [
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hel"},
                        "finish_reason": None,
                    }
                ],
            },
            {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
        ]
    )
    assert text["choices"][0]["message"] == {"role": "assistant", "content": "Hello"}
    assert text["choices"][0]["finish_reason"] == "stop"
    abandoned = assemble_sse(
        [{"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]}]
    )
    assert abandoned["choices"][0]["finish_reason"] is None
    malformed = assemble_sse(
        [
            {"choices": "oops"},
            {"choices": [{"delta": {"tool_calls": [{"index": "x"}]}}]},
            {"choices": [5]},
        ]
    )
    assert malformed["choices"][0]["finish_reason"] is None
