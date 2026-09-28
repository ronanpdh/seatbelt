"""Gemini generateContent wire format -> recorder events, on plain dicts.

The fixture is built from the documented GenerateContentResponse (ai.google.dev/api/
generate-content; see the source map in docs/plans/2026-09-28-gemini-format.md), not captured
from the live API."""

import json
from pathlib import Path
from typing import Any

import pytest

from seatbelt.gateway.formats.gemini import GeminiFormat, assemble_sse
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder

FIXTURE = Path(__file__).parent.parent / "fixtures" / "gemini_refund.json"


def _replies() -> list[dict[str, Any]]:  # fixture JSON
    return json.loads(FIXTURE.read_text())


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "parts": [{"text": text}]}


def _call(name: str, args: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
    fc: dict[str, Any] = {"name": name, "args": args, **({"id": call_id} if call_id else {})}
    return {"role": "model", "parts": [{"functionCall": fc}]}


def _result(name: str, response: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
    fr: dict[str, Any] = {"name": name, "response": response}
    return {
        "role": "user",
        "parts": [{"functionResponse": {**fr, **({"id": call_id} if call_id else {})}}],
    }


def _body(*contents: dict[str, Any], **extra: Any) -> dict[str, Any]:  # request JSON
    return {
        "model": "gemini-2.5-pro",  # put there by the gateway, from the URL
        "contents": [_user("refund 1001"), *contents],
        "generationConfig": {"maxOutputTokens": 500, "thinkingConfig": {"includeThoughts": True}},
        **extra,
    }


def _events(root: Path) -> list[Event]:
    return list(read_events(root / "s.jsonl"))


@pytest.mark.parametrize(
    "cli_id",
    [
        "lookup_order_1789400000_0",  # as Gemini CLI sends it (seen from 0.61.0)
        "lookup_order__lookup_order_1789400000_0",  # its internal form, prefix not stripped
    ],
)
def test_a_call_gemini_cli_names_itself_is_linked_to_its_result_once(
    tmp_path: Path, cli_id: str
) -> None:
    """The API gave the call no id; Gemini CLI made one up, wrote it into its history and
    answers with it."""
    first, second = _replies()
    history = (
        _call("lookup_order", {"order_id": "1001"}, cli_id),
        _result("lookup_order", {"output": "delivered"}, cli_id),
    )
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = GeminiFormat(rec)
        calls = fmt.finish(fmt.begin(_body()), first)
        assert [c.attrs["gen_ai.tool.name"] for c in calls] == ["lookup_order"]
        assert calls[0].attrs["gen_ai.tool.call.arguments"] == {"order_id": "1001"}
        assert fmt.finish(fmt.begin(_body(*history)), second) == []
        fmt.finish(fmt.begin(_body(*history, _user("thanks"))), second)  # resent
    events = _events(tmp_path)
    assert [e.kind for e in events][1:-1] == [
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,  # once, though the result was sent twice
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
    ]
    request, response, call, result = events[1:5]
    assert request.attrs["gen_ai.request.model"] == "gemini-2.5-pro"
    assert request.attrs["gen_ai.provider.name"] == "gemini"
    assert request.attrs["gen_ai.request"]["generationConfig"]["maxOutputTokens"] == 500
    assert "model" not in request.attrs["gen_ai.request"]
    assert response.attrs["gen_ai.response.model"] == "gemini-2.5-pro"
    assert response.attrs["gen_ai.usage.input_tokens"] == 120
    assert response.attrs["gen_ai.usage.input_tokens.cached_tokens"] == 5
    assert response.attrs["gen_ai.usage.output_tokens"] == 31  # answer 21 + thinking 10
    assert response.attrs["gen_ai.usage.output_tokens.reasoning_tokens"] == 10
    assert response.attrs["gen_ai.usage.total_tokens"] == 151
    assert call.parent_id == response.id
    assert result.parent_id == call.id
    assert result.attrs["gen_ai.tool.call.result"] == {"output": "delivered"}
    assert result.attrs["error"] is None


def test_an_api_call_id_is_matched_as_gemini_cli_prefixes_it(tmp_path: Path) -> None:
    first, second = _replies()
    first["candidates"][0]["content"]["parts"][1]["functionCall"]["id"] = "abc"
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = GeminiFormat(rec)
        (call,) = fmt.finish(fmt.begin(_body()), first)
        assert call.attrs["gen_ai.tool.call.id"] == "abc"
        failed = _result("lookup_order", {"error": "order service down"}, "lookup_order__abc")
        fmt.finish(fmt.begin(_body(_call("lookup_order", {}, "lookup_order__abc"), failed)), second)
    (result,) = [e for e in _events(tmp_path) if e.kind is Kind.TOOL_RESULT]
    assert result.parent_id == call.id and result.attrs["error"] == "order service down"


def test_an_old_result_resent_from_before_the_session_claims_no_new_call(tmp_path: Path) -> None:
    """A new session (after the idle window) still carries the old conversation: its results
    name ids this session never saw, so they must not answer the call it just recorded."""
    first, second = _replies()
    old = (
        _call("lookup_order", {"order_id": "999"}, "lookup_order__old"),
        _result("lookup_order", {"output": "old"}, "lookup_order__old"),
    )
    new = (
        _call("lookup_order", {"order_id": "1001"}, "lookup_order__new"),
        _result("lookup_order", {"output": "delivered"}, "lookup_order__new"),
    )
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = GeminiFormat(rec)
        (call,) = fmt.finish(fmt.begin(_body(*old)), first)
        fmt.finish(fmt.begin(_body(*old, *new)), second)
    (result,) = [e for e in _events(tmp_path) if e.kind is Kind.TOOL_RESULT]
    assert result.parent_id == call.id
    assert result.attrs["gen_ai.tool.call.result"] == {"output": "delivered"}


def test_results_without_ids_are_matched_by_name_in_order_and_recorded_once(
    tmp_path: Path,
) -> None:
    """A client that sends no ids at all, e.g. hand-written SDK code: two identical calls
    with identical results are two results, and a resend adds none, even with a new call to
    the same tool open that the old results would otherwise answer."""
    first, second = _replies()
    parts = first["candidates"][0]["content"]["parts"]
    parts.append(parts[1])  # the same call twice
    history = (
        {"role": "model", "parts": parts[1:]},
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "lookup_order", "response": {"output": "x"}}},
                {"functionResponse": {"name": "lookup_order", "response": {"output": "x"}}},
            ],
        },
    )
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = GeminiFormat(rec)
        calls = fmt.finish(fmt.begin(_body()), first)
        assert [c.attrs["gen_ai.tool.call.id"] for c in calls] == [
            "lookup_order#1",
            "lookup_order#2",
        ]
        fmt.finish(fmt.begin(_body(*history)), second)
        (again,) = fmt.finish(fmt.begin(_body(*history)), _replies()[0])  # a third call
        answer = _result("lookup_order", {"output": "y"})
        fmt.finish(
            fmt.begin(_body(*history, {"role": "model", "parts": parts[1:2]}, answer)), second
        )
    results = [e for e in _events(tmp_path) if e.kind is Kind.TOOL_RESULT]
    assert [r.parent_id for r in results] == [*(c.id for c in calls), again.id]
    assert results[-1].attrs["gen_ai.tool.call.result"] == {"output": "y"}


def test_tool_result_calls_name_each_result() -> None:
    body = _body(
        _result("lookup_order", {"output": "x"}, "lookup_order__a"),
        _result("refund", {"output": "y"}),
    )
    assert GeminiFormat.tool_result_calls(body) == [
        ("lookup_order__a", "lookup_order"),
        ("refund", "refund"),
    ]


def test_a_blocked_prompt_and_a_failed_request_are_errors(tmp_path: Path) -> None:
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {}}
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = GeminiFormat(rec)
        assert fmt.finish(fmt.begin(_body()), blocked) == []
        assert fmt.finish(fmt.begin(_body()), None, error="429: quota") == []
    errors = [e.attrs["error"] for e in _events(tmp_path) if e.kind is Kind.MODEL_RESPONSE]
    assert errors == ["prompt blocked: SAFETY", "429: quota"]


def test_a_stream_is_rebuilt_as_gemini_cli_reads_it() -> None:
    def chunk(*parts: dict[str, Any], **extra: Any) -> dict[str, Any]:  # SSE JSON
        return {
            "candidates": [{"content": {"role": "model", "parts": list(parts)}, "index": 0}],
            "modelVersion": "gemini-2.5-pro",
            **extra,
        }

    fc = {"functionCall": {"name": "lookup_order", "args": {"order_id": "1001"}}}
    chunks: list[dict[str, Any]] = [  # SSE JSON
        chunk({"text": "Let me ", "thought": True}),
        chunk({"text": "check.", "thought": True}),
        chunk({"text": "Checking "}, usageMetadata={"promptTokenCount": 120}),
        chunk({"text": "order 1001."}, fc),
        {
            "candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 21},
            "responseId": "resp-01",
        },
    ]
    out = assemble_sse(chunks)
    (candidate,) = out["candidates"]
    assert candidate["content"]["parts"] == [
        {"text": "Let me check.", "thought": True},
        {"text": "Checking order 1001."},
        fc,
    ]
    assert candidate["finishReason"] == "STOP"
    assert out["usageMetadata"] == {"promptTokenCount": 120, "candidatesTokenCount": 21}
    assert out["modelVersion"] == "gemini-2.5-pro" and out["responseId"] == "resp-01"
    assert assemble_sse([]) == {} and assemble_sse([{"candidates": "junk"}]) == {}
