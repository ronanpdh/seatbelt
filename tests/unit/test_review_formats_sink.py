"""Regressions for the review of the wire formats, the sink and redaction: request shapes
the gateway refuses rather than record wrongly, streamed and non-streamed records, secret
formats, and shipping to object storage."""

import base64
import hashlib
import json
import logging
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import botocore.auth
import httpx2
import pytest
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from hypothesis import given, settings
from hypothesis import strategies as st

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer
from seatbelt.gateway.formats import anthropic, gemini, openai_chat
from seatbelt.gateway.formats.anthropic import AnthropicFormat
from seatbelt.gateway.formats.gemini import CodeAssistFormat, GeminiFormat
from seatbelt.gateway.formats.openai_chat import OpenAIChatFormat
from seatbelt.gateway.formats.openai_responses import OpenAIResponsesFormat
from seatbelt.gateway.sink import S3Store, Sink, StoreError, sigv4_headers
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.redact import redact_text
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder

NOW = datetime(2026, 9, 28, 12, 34, 56, tzinfo=UTC)
AK, SK = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"  # fake test credentials


def _events(root: Path) -> list[Event]:
    return list(read_events(root / "s.jsonl"))


# GW-3 / FSR-1: Gemini's snake_case field names


def _gemini(**extra: Any) -> dict[str, Any]:  # request JSON
    return {
        "model": "gemini-2.5-pro",
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 100},
        **extra,
    }


def _part(part: dict[str, Any]) -> dict[str, Any]:
    return _gemini(contents=[{"role": "user", "parts": [{"text": "hi"}, part]}])


@pytest.mark.parametrize(
    "body",
    [
        _gemini(system_instruction={"parts": [{"text": "hidden"}]}),
        _gemini(generation_config={"max_output_tokens": 65536}),
        _gemini(tool_config={"function_calling_config": {"mode": "ANY"}}),
        _gemini(safety_settings=[]),
        _gemini(cached_content="cachedContents/x"),
        _gemini(generationConfig={"max_output_tokens": 65536}),
        _gemini(generationConfig={"candidate_count": 2}),
        _part({"function_response": {"name": "run_shell", "response": {"output": "x"}}}),
        _part({"function_call": {"name": "run_shell", "args": {}}}),
        _part({"inline_data": {"mime_type": "image/png", "data": "AAAA"}}),
        _part({"file_data": {"file_uri": "gs://b/f"}}),
        _part({"executable_code": {"code": "print(1)"}}),
        _part({"code_execution_result": {"output": "1"}}),
    ],
)
def test_gemini_snake_case_aliases_of_fields_read_or_recorded_are_refused(
    body: dict[str, Any],
) -> None:
    reason = GeminiFormat.unrecordable(body)
    assert reason is not None and "camelCase" in reason
    wrapped = {"model": "gemini-2.5-pro", "project": "p", "request": body}
    assert CodeAssistFormat.unrecordable(wrapped) == reason


def test_gemini_keys_the_caller_owns_are_left_alone() -> None:
    """Arguments, results and schemas are the caller's JSON: snake_case there is data."""
    snake: dict[str, Any] = {"max_output_tokens": 1, "system_instruction": "x", "function_call": {}}
    body = _gemini(
        contents=[
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "f", "args": snake}}]},
            {
                "role": "user",
                "parts": [{"functionResponse": {"name": "f", "response": snake}}],
            },
        ],
        tools=[
            {
                "functionDeclarations": [
                    {"name": "f", "parameters": {"type": "object", "properties": snake}}
                ]
            }
        ],
        systemInstruction={"parts": [{"text": "be brief"}]},
        generationConfig={"maxOutputTokens": 100, "candidateCount": 1},
    )
    assert GeminiFormat.unrecordable(body) is None
    assert CodeAssistFormat.unrecordable({"model": "m", "request": body}) is None


# GW-7 / FSR-3: one choice, one candidate


@pytest.mark.parametrize("count", [2, "2", 2.0, "two", [2], {"n": 2}])
def test_gemini_more_than_one_candidate_is_refused(count: Any) -> None:
    body = _gemini(generationConfig={"candidateCount": count})
    assert "candidateCount" in (GeminiFormat.unrecordable(body) or "")
    assert CodeAssistFormat.unrecordable({"model": "m", "request": body}) is not None


@pytest.mark.parametrize("count", [1, "1", 1.0])
def test_gemini_one_candidate_passes(count: Any) -> None:
    assert GeminiFormat.unrecordable(_gemini(generationConfig={"candidateCount": count})) is None


def _chat(**extra: Any) -> dict[str, Any]:  # request JSON
    return {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}], **extra}


@pytest.mark.parametrize("n", [2, "2", 3.0, "x", True])
def test_chat_more_than_one_choice_is_refused(n: Any) -> None:
    assert "n " in (OpenAIChatFormat.unrecordable(_chat(n=n)) or "")


@pytest.mark.parametrize("extra", [{}, {"n": 1}, {"n": None}, {"n": 1.0}])
def test_chat_one_choice_passes(extra: dict[str, Any]) -> None:
    assert OpenAIChatFormat.unrecordable(_chat(**extra)) is None


# GW-4 / FSR-4: legacy function calling, and custom tools


@pytest.mark.parametrize(
    "body",
    [
        _chat(functions=[{"name": "run_shell", "parameters": {}}]),
        _chat(function_call="auto"),
        _chat(function_call={"name": "run_shell"}),
        _chat(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "function", "name": "run_shell", "content": "root"},
            ]
        ),
    ],
)
def test_chat_legacy_function_calling_is_refused(body: dict[str, Any]) -> None:
    assert "legacy" in (OpenAIChatFormat.unrecordable(body) or "")


def test_chat_tools_pass() -> None:
    body = _chat(tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}])
    assert OpenAIChatFormat.unrecordable(body) is None


_CUSTOM = {"id": "call_c", "type": "custom", "custom": {"name": "run_shell", "input": "ls -la"}}


def test_chat_custom_tool_calls_are_recorded_and_named(tmp_path: Path) -> None:
    response = {
        "id": "chatcmpl-1",
        "model": "gpt-5",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [_CUSTOM]},
            }
        ],
    }
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIChatFormat(rec)
        (call,) = fmt.finish(fmt.begin(_chat()), response)
        assert call.attrs["gen_ai.tool.name"] == "run_shell"
        assert call.attrs["gen_ai.tool.call.arguments"] == {"input": "ls -la"}
        history = _chat(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "tool_calls": [_CUSTOM]},
                {"role": "tool", "tool_call_id": "call_c", "content": "total 0"},
            ]
        )
        assert OpenAIChatFormat.tool_result_calls(history) == [("call_c", "run_shell")]
        fmt.begin(history)
    (result,) = [e for e in _events(tmp_path) if e.kind is Kind.TOOL_RESULT]
    assert result.parent_id == call.id


def test_chat_streamed_custom_tool_input_is_assembled(tmp_path: Path) -> None:
    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-1",
            "model": "gpt-5",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    first: dict[str, Any] = {
        "index": 0,
        "id": "call_c",
        "type": "custom",
        "custom": {"name": "run_shell"},
    }
    chunks: list[dict[str, Any]] = [
        chunk({"role": "assistant", "tool_calls": [{**first, "custom": {**first["custom"]}}]}),
        chunk({"tool_calls": [{"index": 0, "custom": {"input": "ls "}}]}),
        chunk({"tool_calls": [{"index": 0, "custom": {"input": "-la"}}]}),
        chunk({}, "tool_calls"),
    ]
    message = openai_chat.assemble_sse(chunks)["choices"][0]["message"]
    assert message["tool_calls"] == [_CUSTOM]
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIChatFormat(rec)
        (call,) = fmt.finish(fmt.begin(_chat()), openai_chat.assemble_sse(chunks))
    assert call.attrs["gen_ai.tool.name"] == "run_shell"
    assert call.attrs["gen_ai.tool.call.arguments"] == {"input": "ls -la"}


def test_chat_streamed_function_calls_keep_their_shape() -> None:
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "f"}}]}}]},
    ]
    (tc,) = openai_chat.assemble_sse(chunks)["choices"][0]["message"]["tool_calls"]
    assert tc == {"id": "c", "type": "function", "function": {"name": "f", "arguments": ""}}


# FSR-5: streamed thinking, signatures, citations and refusals


_THINKING = "The user wants a refund. Order 1001 was delivered."
_SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds"


def _anthropic_message() -> dict[str, Any]:  # response JSON
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [
            {"type": "thinking", "thinking": _THINKING, "signature": _SIGNATURE},
            {"type": "text", "text": "Refunded."},
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def _anthropic_stream() -> list[dict[str, Any]]:  # SSE events
    start: dict[str, Any] = {**_anthropic_message(), "content": [], "stop_reason": None}
    start["usage"] = {"input_tokens": 10, "output_tokens": 1}
    return [
        {"type": "message_start", "message": start},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": _THINKING[:20]},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": _THINKING[20:]},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": _SIGNATURE},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Refunded."},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 20},
        },
        {"type": "message_stop"},
    ]


def test_a_streamed_thinking_response_is_recorded_as_the_same_response_unstreamed(
    tmp_path: Path,
) -> None:
    body: dict[str, Any] = {"model": "claude-sonnet-5", "max_tokens": 100, "messages": []}
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = AnthropicFormat(rec)
        fmt.finish(fmt.begin(body), _anthropic_message())
        fmt.finish(fmt.begin(body), anthropic.assemble_sse(_anthropic_stream()))
    whole, streamed = [e.attrs for e in _events(tmp_path) if e.kind is Kind.MODEL_RESPONSE]
    assert streamed == whole
    assert streamed["gen_ai.response"]["content"][0]["thinking"] == _THINKING
    assert streamed["error"] is None


def test_streamed_citations_and_refusals_are_kept() -> None:
    citation = {"type": "char_location", "cited_text": "x", "document_index": 0}
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "citations_delta", "citation": citation},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "citations_delta", "citation": citation},
        },
    ]
    assert anthropic.assemble_sse(events)["content"][0]["citations"] == [citation, citation]
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"role": "assistant", "refusal": "I can't "}}]},
        {"choices": [{"delta": {"refusal": "help with that."}, "finish_reason": "stop"}]},
    ]
    message = openai_chat.assemble_sse(chunks)["choices"][0]["message"]
    assert message["refusal"] == "I can't help with that."


# FSR-6: a stream that ended without a stop reason is an error


def _error_of(tmp_path: Path, fmt_cls: Any, body: dict[str, Any], response: Any) -> Any:
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = fmt_cls(rec)
        fmt.finish(fmt.begin(body), response)
    (answer,) = [e for e in _events(tmp_path) if e.kind is Kind.MODEL_RESPONSE]
    return answer.attrs["error"]


def test_an_anthropic_error_event_mid_stream_is_recorded(tmp_path: Path) -> None:
    events = [
        *_anthropic_stream()[:3],
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    ]
    response = anthropic.assemble_sse(events)
    error = _error_of(tmp_path, AnthropicFormat, {"model": "m"}, response)
    assert error == "overloaded_error: Overloaded"


def test_an_anthropic_stream_without_a_stop_reason_is_an_error(tmp_path: Path) -> None:
    response = anthropic.assemble_sse(_anthropic_stream()[:4])
    error = _error_of(tmp_path, AnthropicFormat, {"model": "m"}, response)
    assert error == "response ended without a stop reason"


def test_chat_error_chunks_and_missing_finish_reasons_are_errors(tmp_path: Path) -> None:
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"error": {"type": "server_error", "message": "The server had an error"}},
    ]
    error = _error_of(tmp_path / "a", OpenAIChatFormat, _chat(), openai_chat.assemble_sse(chunks))
    assert error == "server_error: The server had an error"
    error = _error_of(
        tmp_path / "b", OpenAIChatFormat, _chat(), openai_chat.assemble_sse(chunks[:1])
    )
    assert error == "response ended without a finish reason"
    done: list[dict[str, Any]] = [
        *chunks[:1],
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    assert (
        _error_of(tmp_path / "c", OpenAIChatFormat, _chat(), openai_chat.assemble_sse(done)) is None
    )


def test_gemini_error_chunks_and_missing_finish_reasons_are_errors(tmp_path: Path) -> None:
    text = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hel"}]}}]}
    error_chunk = {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}
    response = gemini.assemble_sse([text, error_chunk])
    assert _error_of(tmp_path / "a", GeminiFormat, _gemini(), response) == "UNAVAILABLE: overloaded"
    response = gemini.assemble_sse([text])
    error = _error_of(tmp_path / "b", GeminiFormat, _gemini(), response)
    assert error == "response ended without a finish reason"
    done = gemini.assemble_sse([text, {"candidates": [{"finishReason": "STOP"}]}])
    assert _error_of(tmp_path / "c", GeminiFormat, _gemini(), done) is None


# FSR-7: Gemini results whose call's arguments held a secret


def test_a_gemini_result_is_matched_to_a_call_whose_arguments_were_redacted(
    tmp_path: Path,
) -> None:
    token = "ghp_" + secrets.token_hex(18)  # a fake GitHub token, redacted when recorded
    args = {"cmd": f"git clone https://{token}@github.com/x/y"}
    response = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"functionCall": {"name": "sh", "args": args}}],
                },
                "finishReason": "STOP",
            }
        ]
    }
    call = {
        "role": "model",
        "parts": [{"functionCall": {"id": "cli-1", "name": "sh", "args": args}}],
    }
    result = {
        "role": "user",
        "parts": [
            {"functionResponse": {"id": "cli-1", "name": "sh", "response": {"output": "ok"}}}
        ],
    }
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = GeminiFormat(rec)
        (called,) = fmt.finish(fmt.begin(_gemini()), response)
        assert token not in json.dumps(called.attrs)
        body = _gemini()
        fmt.begin({**body, "contents": [*body["contents"], call, result]})
    (returned,) = [e for e in _events(tmp_path) if e.kind is Kind.TOOL_RESULT]
    assert returned.parent_id == called.id


# FSR-11: a `type` that is not a string


@pytest.mark.parametrize("kind", [{}, [], {"a": 1}, 1, None])
def test_a_responses_item_type_that_is_not_a_string_is_skipped(tmp_path: Path, kind: Any) -> None:
    body = {"model": "gpt-5", "input": [{"type": kind, "call_id": "c"}]}
    assert OpenAIResponsesFormat.tool_result_calls(body) == []
    with Recorder.start(tmp_path, agent_id="gw", run_id="s") as rec:
        fmt = OpenAIResponsesFormat(rec)
        call = fmt.begin(body)
        response = {"status": "completed", "output": [{"type": kind, "call_id": "c"}]}
        assert fmt.finish(call, response) == []


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=8),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=20,
)
_KEYS = st.sampled_from(
    [
        "type",
        "input",
        "output",
        "messages",
        "contents",
        "parts",
        "choices",
        "candidates",
        "content",
        "tool_calls",
        "call_id",
        "functionCall",
        "functionResponse",
        "name",
        "id",
        "role",
        "status",
        "finish_reason",
        "finishReason",
        "stop_reason",
        "message",
        "request",
        "response",
        "custom",
        "function",
        "n",
        "generationConfig",
        "candidateCount",
    ]
)
_BODY = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(_KEYS, inner, max_size=5),
    max_leaves=30,
)


@settings(max_examples=150, deadline=None)
@given(st.dictionaries(_KEYS, _BODY, max_size=6) | st.dictionaries(st.text(), _JSON))
def test_every_format_takes_any_json_object(
    tmp_path_factory: pytest.TempPathFactory, body: dict[str, Any]
) -> None:
    """What fuzz/fuzz_parsers.py `body` checks: checks and recording never raise."""
    root = tmp_path_factory.mktemp("any")
    with Recorder.start(root, agent_id="gw", run_id="s") as rec:
        for fmt_cls in (
            AnthropicFormat,
            OpenAIChatFormat,
            OpenAIResponsesFormat,
            GeminiFormat,
            CodeAssistFormat,
        ):
            fmt_cls.unrecordable(body)
            fmt_cls.tool_result_calls(body)
            fmt = fmt_cls(rec)
            fmt.finish(fmt.begin(body), body)


def test_responses_background_is_refused_by_the_format() -> None:
    assert OpenAIResponsesFormat.unrecordable({"model": "m", "background": True}) is not None
    assert OpenAIResponsesFormat.unrecordable({"model": "m", "background": False}) is None
    assert AnthropicFormat.unrecordable({"model": "m", "n": 5}) is None


# FSR-2: secret formats


_SBK = "sbk_" + secrets.token_urlsafe(32)  # the shape of a key seatbelt issues; fake
# fake secrets, assembled here so that no literal in the source looks like a real key to
# secret scanners
_GOOGLE = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
_STRIPE_BODY = "Zq9Xw8Vu7Ts6Rp5Oq4Nr3Ms2"
_WEBHOOK = "whsec" + "_abcdefghijklmnop1234"
_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"ANTHROPIC_AUTH_TOKEN={_SBK}", _SBK),
        (f'key = "{_SBK}"', _SBK),
        (f"https://gw.corp.example/_seatbelt/{_SBK}/v1", _SBK),
        (
            "GEMINI_API_KEY=" + _GOOGLE,
            _GOOGLE,
        ),
        (
            "?key=" + _GOOGLE + "&alt=sse",
            "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        ),
        ("AWS_ACCESS_KEY_ID=" + "ASIA" + "ABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"),
        ("id_token: " + _JWT, _JWT),
        ("sk_" + "live_" + _STRIPE_BODY, _STRIPE_BODY),
        ("rk_" + "test_" + _STRIPE_BODY, _STRIPE_BODY),
        ("xox" + "b-1234567890-abcdefghijkl", "1234567890-abcdefghijkl"),
        ("git clone https://deploy:hunter2hunter2@git.corp.example/x.git", "hunter2hunter2"),
        ("postgres://admin:s3cr3t@db.internal:5432/app", "s3cr3t"),
        ("DATABASE_PASSWORD=correct-horse-battery-staple", "correct-horse-battery-staple"),
        ("export AWS_SECRET_ACCESS_KEY='wJalrXUtnFEMI/K7MDENG/bPxRfiCYzzzz'", "wJalrXUtnFEMI"),
        ('STRIPE_WEBHOOK_SECRET="' + _WEBHOOK + '"', _WEBHOOK),
    ],
)
def test_redaction_catches_more_secret_formats(text: str, secret: str) -> None:
    redacted = redact_text(text)
    assert secret not in redacted
    assert "[REDACTED:" in redacted


_PEM = (  # a fake key's shape
    "-----BEGIN {0}-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
    "b3BlbnNzaC1rZXk=\n"
    "-----END {0}-----"
)


@pytest.mark.parametrize(
    "label", ["PRIVATE KEY", "RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "ENCRYPTED PRIVATE KEY"]
)
def test_redaction_catches_private_keys_whole(label: str) -> None:
    text = "before\n" + _PEM.format(label) + "\nafter"
    assert redact_text(text) == "before\n[REDACTED:private_key]\nafter"
    escaped = json.dumps({"private_key": _PEM.format(label) + "\n"})  # a service account file
    assert "MIIEvQ" not in redact_text(escaped) and "b3Blbn" not in redact_text(escaped)
    cut = _PEM.format(label).split("-----END")[0]  # `head` of a key file
    assert "MIIEvQ" not in redact_text(cut) and "b3Blbn" not in redact_text(cut)


def test_redaction_keeps_what_is_not_secret_around_a_secret() -> None:
    assert (
        redact_text("git clone https://deploy:hunter2hunter2@git.corp.example/x.git")
        == "git clone https://[REDACTED:url_credentials]@git.corp.example/x.git"
    )
    assert (
        redact_text("DATABASE_PASSWORD=correct-horse-battery-staple\nDEBUG=1")
        == "DATABASE_PASSWORD=[REDACTED:env_secret]\nDEBUG=1"
    )
    assert (
        redact_text(f"ANTHROPIC_AUTH_TOKEN={_SBK}")
        == "ANTHROPIC_AUTH_TOKEN=[REDACTED:seatbelt_key]"
    )


@pytest.mark.parametrize(
    "text",
    [
        "MAX_TOKENS=4096",
        "MAX_OUTPUT_TOKENS=1234567890123456789",
        "token = getTokenFromEnv()",
        "api_key = os.environ['OPENAI_API_KEY']",
        "SECRET_KEY = settings.load_secret_key_from_vault()",
        "KEYBOARD_LAYOUT=us-international-alt",
        "SECRETS_FILE=/etc/app/secrets.env.production",
        "SECRET_KEY_BASE=${SECRET_KEY_BASE_FROM_VAULT}",
        "PASSWORD_MIN_LENGTH=16",
        "https://example.com:8080/path@x",
        "mail alice@example.com or see http://docs.example.com/a:b@c",
        "ssh git@github.com:org/repo.git",
        "the model said eyJ is how a JWT starts",
        "desk_live_view and sk_test_short",
        "xoxo-hugs and kisses",
        "-----BEGIN PUBLIC KEY-----",
        "AIza is a prefix",
        "sbk_short",
    ],
)
def test_redaction_leaves_code_config_and_prose_alone(text: str) -> None:
    assert redact_text(text) == text


# FSR-8: a signature written after its ledger shipped


class FakeStore:
    def __init__(self, refuse: str = "") -> None:
        self.puts: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.refuse = refuse
        self.done = threading.Event()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.puts.append(key)
        if self.refuse and key.startswith(self.refuse):
            raise StoreError("400: EntityTooLarge")
        self.objects[key] = data
        if key.startswith("good"):
            self.done.set()


def _closed_ledger(root: Path, run_id: str, signer: Signer | None) -> Path:
    with Recorder.start(root, agent_id="gateway", run_id=run_id, signer=signer) as rec:
        rec.user_message("u", "hi")
    return root / f"{run_id}.jsonl"


def _drain(sink: Sink) -> None:
    sink.start()
    assert sink.stop(timeout=5) == 0


def test_a_signature_written_after_its_ledger_shipped_is_shipped_too(tmp_path: Path) -> None:
    signer = Signer.generate()
    path = _closed_ledger(tmp_path, "r1", signer)
    signature = sidecar(path).read_bytes()
    sidecar(path).unlink()  # as a gateway killed while signing leaves it
    store = FakeStore()
    sink = Sink(store, tmp_path, prefix="runs/")
    assert sink.catch_up() == 1
    _drain(sink)
    assert store.puts == ["runs/r1.jsonl"]
    assert sink.shipped(path) and Sink(store, tmp_path, prefix="runs/").catch_up() == 0
    sidecar(path).write_bytes(signature)  # the operator runs `seatbelt attest`
    sink = Sink(store, tmp_path, prefix="runs/")
    assert not sink.shipped(path)
    assert sink.catch_up() == 1
    _drain(sink)
    assert store.puts == ["runs/r1.jsonl", "runs/r1.attest.json"]  # the ledger not again
    assert store.objects["runs/r1.attest.json"] == signature
    mark = json.loads((tmp_path / ".shipped" / "r1").read_text())
    assert set(mark) == {"runs/r1.jsonl", "runs/r1.attest.json"}
    assert sink.shipped(path) and sink.catch_up() == 0


# FSR-9: a checksum, and one ledger the store refuses does not block the rest


def test_puts_carry_a_signed_content_md5(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx2.Request] = []

    def ok(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    store = S3Store(
        "https://fsn1.your-objectstorage.com",
        "ledgers",
        "fsn1",
        AK,
        SK,
        httpx2.MockTransport(ok),
        lambda: NOW,
    )
    store.put("runs/r.jsonl", b'{"seq":0}\n', "application/x-ndjson")
    (sent,) = seen
    md5 = base64.b64encode(hashlib.md5(b'{"seq":0}\n', usedforsecurity=False).digest()).decode()
    assert sent.headers["content-md5"] == md5
    assert "content-md5" in sent.headers["authorization"].split("SignedHeaders=")[1]
    assert sent.headers["authorization"] == _botocore(str(sent.url), sent, monkeypatch)


def _botocore(url: str, sent: httpx2.Request, monkeypatch: pytest.MonkeyPatch) -> str:
    """botocore's signature for what was sent: the oracle."""
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: NOW.replace(tzinfo=None))
    request = AWSRequest(
        method="PUT",
        url=url,
        data=sent.content,
        headers={
            k: sent.headers[k] for k in ("content-type", "content-md5", "x-amz-content-sha256")
        },
    )
    botocore.auth.SigV4Auth(Credentials(AK, SK), "s3", "fsn1").add_auth(request)
    return str(request.headers["Authorization"])


def test_a_ledger_the_store_keeps_refusing_does_not_hold_up_the_queue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad = _closed_ledger(tmp_path, "bad", None)
    good = _closed_ledger(tmp_path, "good", None)
    store = FakeStore(refuse="bad")
    sink = Sink(store, tmp_path, backoff=(0.01,), attempts=3)
    sink.start()
    with caplog.at_level(logging.ERROR, logger="seatbelt.gateway.sink"):
        sink.ship(bad)
        sink.ship(good)
        assert store.done.wait(5)  # shipped behind the refused one
        assert sink.stop(timeout=0.5) == 1  # the refused one, left for the next start
    assert sink.shipped(good) and not sink.shipped(bad)
    assert store.puts[:3] == ["bad.jsonl"] * 3
    assert any(r.levelno == logging.ERROR and "bad.jsonl" in r.getMessage() for r in caplog.records)


# FSR-10: the endpoint


@pytest.mark.parametrize(
    "url",
    [
        "http://fsn1.your-objectstorage.com",
        "ftp://fsn1.your-objectstorage.com",
        "fsn1.your-objectstorage.com",
        "https://fsn1.your-objectstorage.com/ledgers",
        "https://fsn1.your-objectstorage.com?x=1",
        "https://fsn1.your-objectstorage.com#x",
        "https://AKID:hunter2hunter2@fsn1.your-objectstorage.com",
        "https://fsn1.your-objectstorage.com:99999",
        "https://",
    ],
)
def test_a_sink_url_that_is_not_https_host_and_port_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="sink url must be") as caught:
        S3Store(url, "ledgers", "fsn1", AK, SK)
    assert "hunter2" not in str(caught.value)


def test_http_is_accepted_only_when_allowed() -> None:
    S3Store("http://minio.internal:9000", "ledgers", "us-east-1", AK, SK, allow_http=True).close()
    S3Store("https://fsn1.your-objectstorage.com/", "ledgers", "fsn1", AK, SK).close()


@pytest.mark.parametrize(
    ("endpoint", "host"),
    [
        ("https://FSN1.your-objectstorage.com:443", "ledgers.fsn1.your-objectstorage.com"),
        ("https://s3.corp.example:9000", "ledgers.s3.corp.example:9000"),
    ],
)
def test_the_host_signed_is_the_host_sent(
    endpoint: str, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx2.Request] = []

    def ok(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    store = S3Store(endpoint, "ledgers", "fsn1", AK, SK, httpx2.MockTransport(ok), lambda: NOW)
    store.put("r.jsonl", b"x", "application/x-ndjson")
    (sent,) = seen
    assert sent.headers["host"] == host
    # botocore drops a default port too: the URL as configured signs as the one sent
    as_configured = f"https://ledgers.{endpoint.split('://')[1].lower()}/r.jsonl"
    assert sent.headers["authorization"] == _botocore(as_configured, sent, monkeypatch)
    ours = sigv4_headers("PUT", as_configured, b"x", {}, "fsn1", AK, SK, NOW)
    theirs = sigv4_headers("PUT", str(sent.url), b"x", {}, "fsn1", AK, SK, NOW)
    assert ours == theirs


def test_the_sink_config_opts_into_plain_http(tmp_path: Path) -> None:
    from seatbelt.gateway.config import GatewayConfig, SinkConfig
    from seatbelt.gateway.serve import make_sink

    env = {"SEATBELT_SINK_ACCESS_KEY": "a", "SEATBELT_SINK_SECRET_KEY": "b"}
    plain = SinkConfig(url="http://minio.internal:9000", bucket="b", region="r")
    cfg = GatewayConfig(ledgers=tmp_path, sink=plain)
    with pytest.raises(ValueError):
        make_sink(cfg, env)
    allowed = cfg.model_copy(update={"sink": plain.model_copy(update={"allow_http": True})})
    assert make_sink(allowed, env) is not None
