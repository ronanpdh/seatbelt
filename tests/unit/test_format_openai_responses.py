"""OpenAI Responses wire format -> recorder events, on plain dicts.

The fixture is built from the openai-python Responses types (see the source map in
docs/plans/2026-09-28-responses-format.md), not captured from the live API."""

import json
from pathlib import Path
from typing import Any

from seatbelt.gateway.formats.openai_responses import OpenAIResponsesFormat, assemble_sse
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder

FIXTURE = Path(__file__).parent.parent / "fixtures" / "openai_responses_refund.json"


def _replies() -> list[dict[str, Any]]:  # fixture JSON
    return json.loads(FIXTURE.read_text())


def _body(*items: dict[str, Any]) -> dict[str, Any]:  # request JSON
    return {
        "model": "gpt-5",
        "input": [{"role": "user", "content": "refund 1001"}, *items],
        "store": False,
        "stream": True,
        "max_output_tokens": 500,
    }


def test_function_calls_and_their_outputs_are_linked(tmp_path: Path) -> None:
    first, second = _replies()
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIResponsesFormat(rec)
        calls = fmt.finish(fmt.begin(_body()), first)
        assert [c.attrs["gen_ai.tool.name"] for c in calls] == ["lookup_order"]
        assert calls[0].attrs["gen_ai.tool.call.arguments"] == {"order_id": "1001"}
        output = {"type": "function_call_output", "call_id": "call_01", "output": "delivered"}
        # the whole history each turn, as Codex sends it: the call, then its output
        assert fmt.finish(fmt.begin(_body(first["output"][1], output)), second) == []
        fmt.finish(fmt.begin(_body(first["output"][1], output)), second)  # resent again
    events = list(read_events(tmp_path / "s.jsonl"))
    assert [e.kind for e in events][1:-1] == [
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,  # once, though the output was sent twice
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
    ]
    request, response, call, result = events[1:5]
    assert "stream" not in request.attrs["gen_ai.request"]
    assert request.attrs["gen_ai.request"]["max_output_tokens"] == 500
    assert request.attrs["gen_ai.provider.name"] == "openai"
    assert response.attrs["gen_ai.response.model"] == "gpt-5-2025-08-07"
    assert response.attrs["gen_ai.usage.input_tokens"] == 120
    assert response.attrs["gen_ai.usage.output_tokens"] == 31
    assert response.attrs["gen_ai.usage.input_tokens.cached_tokens"] == 5
    assert response.attrs["gen_ai.usage.output_tokens.reasoning_tokens"] == 2
    assert response.attrs["error"] is None
    assert call.parent_id == response.id and call.attrs["gen_ai.tool.call.id"] == "call_01"
    assert result.parent_id == call.id and result.attrs["gen_ai.tool.call.result"] == "delivered"


def test_every_client_run_tool_type_is_a_tool_call(tmp_path: Path) -> None:
    output: list[dict[str, Any]] = [  # response JSON
        {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch", "input": "*** Begin"},
        {
            "type": "local_shell_call",
            "call_id": "c2",
            "action": {"type": "exec", "command": ["ls"]},
        },
        {"type": "shell_call", "call_id": "c3", "action": {"commands": ["pwd"]}},
        {"type": "apply_patch_call", "call_id": "c4", "operation": {"type": "update_file"}},
        {"type": "computer_call", "call_id": "c5", "action": {"type": "click"}},
        {"type": "web_search_call", "id": "ws1", "status": "completed"},  # run by the provider
    ]
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIResponsesFormat(rec)
        calls = fmt.finish(fmt.begin(_body()), {"status": "completed", "output": output})
        assert [
            (c.attrs["gen_ai.tool.name"], c.attrs["gen_ai.tool.call.arguments"]) for c in calls
        ] == [
            ("apply_patch", {"input": "*** Begin"}),
            ("local_shell", {"action": {"type": "exec", "command": ["ls"]}}),
            ("shell", {"action": {"commands": ["pwd"]}}),
            ("apply_patch", {"operation": {"type": "update_file"}}),
            ("computer", {"action": {"type": "click"}}),
        ]
        answers = _body(
            {"type": "custom_tool_call_output", "call_id": "c1", "output": "patched"},
            {"type": "function_call_output", "call_id": "c2", "output": "a.txt"},  # Codex's form
            {"type": "shell_call_output", "call_id": "c3", "output": []},
            {"type": "apply_patch_call_output", "call_id": "c4", "status": "failed"},
            {"type": "computer_call_output", "call_id": "c5", "output": {}},
        )
        fmt.begin(answers)
    results = [e for e in read_events(tmp_path / "s.jsonl") if e.kind is Kind.TOOL_RESULT]
    assert len(results) == 5
    assert [r.attrs["error"] for r in results] == [None, None, None, "tool reported failure", None]


def test_tool_result_calls_name_each_result_from_the_input() -> None:
    body = _body(
        {"type": "function_call", "call_id": "a", "name": "run_shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "x"},
        {"type": "local_shell_call", "call_id": "b", "action": {}},
        {"type": "function_call_output", "call_id": "b", "output": "y"},
        {"type": "function_call_output", "call_id": "gone", "output": "z"},  # chained, no call
    )
    assert OpenAIResponsesFormat.tool_result_calls(body) == [
        ("a", "run_shell"),
        ("b", "local_shell"),
        ("gone", None),
    ]
    assert OpenAIResponsesFormat.tool_result_calls({"model": "m", "input": "hi"}) == []


def test_unfinished_calls_and_failed_responses_record_no_tool_calls(tmp_path: Path) -> None:
    first, _ = _replies()
    cut = {**first, "status": "in_progress"}
    cut["output"] = [{**first["output"][1], "status": "in_progress"}]
    failed: dict[str, Any] = {  # response JSON
        "status": "failed",
        "error": {"code": "server_error", "message": "boom"},
        "output": [],
        "usage": None,
    }
    incomplete = {
        **first,
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIResponsesFormat(rec)
        assert fmt.finish(fmt.begin(_body()), cut) == []
        assert fmt.finish(fmt.begin(_body()), failed) == []
        assert fmt.finish(fmt.begin(_body()), None, error="502: upstream") == []
        # an incomplete response is not an error, and its completed items are still calls
        assert len(fmt.finish(fmt.begin(_body()), incomplete)) == 1
        bad: dict[str, Any] = {"output": "oops", "usage": 5, "model": 7}  # JSON
        fmt.finish(fmt.begin({"model": "m", "input": 5}), bad)
    responses = [e for e in read_events(tmp_path / "s.jsonl") if e.kind is Kind.MODEL_RESPONSE]
    assert [r.attrs["error"] for r in responses] == [
        "response did not complete (status in_progress)",
        "response failed: server_error: boom",
        "502: upstream",
        None,
        "response did not complete (status unknown)",
    ]


def _stream_of(response: dict[str, Any]) -> list[dict[str, Any]]:  # SSE JSON
    item = response["output"][1]
    started = {**item, "arguments": "", "status": "in_progress"}
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "status": "in_progress", "output": [], "usage": None},
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 1,
            "item": started,
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 2,
            "output_index": 1,
            "delta": '{"order_id": ',
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 3,
            "output_index": 1,
            "item": item,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 4,
            "output_index": 0,
            "item": response["output"][0],
        },
        {"type": "response.completed", "sequence_number": 5, "response": response},
    ]


def test_assemble_sse_takes_the_completed_response() -> None:
    first, _ = _replies()
    assert assemble_sse(_stream_of(first)) == first


def test_assemble_sse_fills_a_terminal_response_without_output() -> None:
    first, _ = _replies()
    events = _stream_of(first)
    events[-1] = {**events[-1], "response": {**first, "output": []}}
    assert assemble_sse(events)["output"] == first["output"]  # in output_index order


def test_assemble_sse_on_a_cut_off_stream_keeps_what_arrived(tmp_path: Path) -> None:
    first, second = _replies()
    events = _stream_of(first)[:3]  # created, item added, a delta: no done, no completed
    message = second["output"][0]
    events += [
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {**message, "content": [], "status": "in_progress"},
        },
        {"type": "response.output_text.delta", "output_index": 2, "delta": "Order 1001 "},
        {"type": "response.output_text.delta", "output_index": 2, "delta": "was"},
    ]
    out = assemble_sse(events)
    assert out["status"] == "in_progress" and out["id"] == "resp_01"
    assert out["output"][0]["status"] == "in_progress"  # the call, as it started
    assert out["output"][1]["content"] == [{"type": "output_text", "text": "Order 1001 was"}]
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIResponsesFormat(rec)
        assert fmt.finish(fmt.begin(_body()), out) == []  # a started call is not recorded


def test_assemble_sse_reads_either_error_event_shape() -> None:
    flat = [{"type": "error", "code": "server_error", "message": "boom", "param": None}]
    nested = [{"type": "error", "error": {"type": "server_error", "code": "x", "message": "boom"}}]
    assert assemble_sse(flat)["error"] == {"code": "server_error", "message": "boom", "param": None}
    assert assemble_sse(nested)["error"]["message"] == "boom"
    assert assemble_sse(flat)["status"] == "failed"
    assert assemble_sse([]) == {"object": "response", "output": []}
    malformed: list[dict[str, Any]] = [  # SSE JSON a proxy mangled
        {"type": "response.output_item.done", "output_index": "x", "item": {}},
        {"type": "response.completed", "response": "oops"},
    ]
    assert assemble_sse(malformed) == {}
