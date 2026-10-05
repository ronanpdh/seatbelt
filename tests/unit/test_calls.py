"""`seatbelt calls` and the numbers behind it: each model call's messages, tools, request size
and tokens, with no prompt or tool text."""

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from seatbelt.cli import app
from seatbelt.gateway.launcher import tool_search_warning
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder
from seatbelt.report.calls import (
    INLINE_MCP_WARN,
    calls,
    heaviest_inline_mcp,
    requested,
)

PROMPT = "summarise the quarterly numbers for ACME"  # must never be shown


def _tool(name: str, deferred: bool = False, pad: int = 100) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": name,
        "description": "x" * pad,
        "input_schema": {"type": "object", "properties": {}},
    }
    if deferred:
        tool["defer_loading"] = True
    return tool


def _mcp_tools(n: int, pad: int, deferred: bool = False) -> list[dict[str, Any]]:
    return [_tool(f"mcp__big__tool_{i}", deferred, pad) for i in range(n)]


def _ledger(root: Path, requests: list[dict[str, Any]], model: str = "claude-opus-5-5") -> Path:
    """A run with one model call per request: answered with cache usage, except that a
    request with `"fail": True` gets an error and one with `"open": True` no response."""
    with Recorder.start(root, "gateway", run_id="r1") as rec:
        for body in requests:
            fail, still_open = body.pop("fail", False), body.pop("open", False)
            call = rec.model_requested(model, body, provider="anthropic")
            if still_open:
                continue
            if fail:
                call.respond({}, error="429: rate_limit_error")
                continue
            usage = {
                "input_tokens": 12,
                "cache_creation_input_tokens": 300,
                "cache_read_input_tokens": 4000,
                "output_tokens": 7,
            }
            call.respond({"content": []}, usage, response_model=model)
    return root / "r1.jsonl"


def _messages(n: int) -> list[dict[str, Any]]:
    return [{"role": "user", "content": PROMPT}] * n


def test_each_call_is_counted_in_order_with_its_usage_and_error(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path,
        [
            {"messages": _messages(1), "fail": True},
            {
                "messages": _messages(2),
                "tools": [_tool("Bash"), *_mcp_tools(3, 100), *_mcp_tools(2, 100, True)],
            },
            {"messages": _messages(4), "open": True},
        ],
    )
    first, second, third = calls(read_events(ledger))
    assert (first.messages, first.tools, first.error) == (1, 0, "429: rate_limit_error")
    assert first.usage == {}
    assert (second.messages, second.tools, second.mcp, second.deferred) == (2, 6, 5, 2)
    assert second.inline_mcp == 3 and second.error is None
    assert second.usage == {
        "input_tokens": 12,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 4000,
        "output_tokens": 7,
    }
    assert second.request_bytes > second.inline_mcp_bytes > 0
    assert (third.messages, third.usage, third.error) == (4, {}, "no response recorded")


def test_other_formats_count_their_turns_and_tools(tmp_path: Path) -> None:
    gemini = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT}]}] * 3,
        "tools": [{"functionDeclarations": [{"name": "a"}, {"name": "b"}]}],
    }
    responses = {"input": PROMPT, "tools": [{"type": "function", "name": "lookup"}]}
    chat = {
        "messages": _messages(2),
        "tools": [{"type": "function", "function": {"name": "mcp__x__y"}}],
    }
    ledger = _ledger(tmp_path, [gemini, responses, chat], model="gpt-5")
    g, r, c = calls(read_events(ledger))
    assert (g.messages, g.tools) == (3, 2)
    assert (r.messages, r.tools) == (1, 1)
    assert (c.messages, c.tools, c.mcp) == (2, 1, 1)


def test_heavy_inline_mcp_tools_are_found_and_deferred_ones_are_not(tmp_path: Path) -> None:
    pad = INLINE_MCP_WARN // 50  # 60 tools of this size are past the warning, together
    ledger = _ledger(
        tmp_path,
        [
            {"messages": _messages(1)},  # a side request, no tools
            {"messages": _messages(2), "tools": _mcp_tools(60, pad, deferred=True)},
            {"messages": _messages(2), "tools": _mcp_tools(60, pad)},
        ],
    )
    made = calls(read_events(ledger))
    heavy = heaviest_inline_mcp(made)
    assert heavy is not None and heavy.inline_mcp == 60 and heavy.deferred == 0
    assert heaviest_inline_mcp(made[:2]) is None  # all deferred: nothing to say
    assert len(list(requested(read_events(ledger), limit=2))) == 2


def test_cli_calls_prints_numbers_and_never_the_prompt(tmp_path: Path) -> None:
    pad = INLINE_MCP_WARN // 50
    ledger = _ledger(
        tmp_path,
        [
            {"messages": _messages(1), "fail": True},
            {"messages": _messages(2), "tools": _mcp_tools(60, pad)},
        ],
    )
    r = CliRunner().invoke(app, ["calls", str(ledger)])
    assert r.exit_code == 0, r.output
    assert PROMPT not in r.output and "mcp__big" not in r.output
    assert "4000" in r.output and "rate_limit_error" in r.output
    assert "2 model calls" in r.output
    assert "60 MCP tools were sent in full" in r.output and "ENABLE_TOOL_SEARCH" in r.output


def test_cli_calls_refuses_an_altered_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [{"messages": _messages(1)}])
    lines = ledger.read_text().splitlines()
    event = json.loads(lines[1])
    event["attrs"]["gen_ai.request.model"] = "something-else"
    lines[1] = json.dumps(event)
    ledger.write_text("\n".join(lines) + "\n")
    r = CliRunner().invoke(app, ["calls", str(ledger)])
    assert r.exit_code == 1 and "BROKEN" in r.output


def test_the_run_warning_names_the_run_and_what_turned_tool_search_off(tmp_path: Path) -> None:
    pad = INLINE_MCP_WARN // 50
    heavy = _ledger(tmp_path / "a", [{"messages": _messages(2), "tools": _mcp_tools(60, pad)}])
    light = _ledger(tmp_path / "b", [{"messages": _messages(2), "tools": _mcp_tools(3, 100)}])
    assert tool_search_warning([light], {}, "claude-1") is None
    warning = tool_search_warning([light, heavy], {}, "claude-1")
    assert warning is not None and warning.startswith("seatbelt: 60 MCP tools")
    assert warning.endswith("each request: seatbelt calls claude-1")
    assert "It was set" not in warning
    on = tool_search_warning([heavy], {"ENABLE_TOOL_SEARCH": "true"}, "claude-1")
    assert on is not None and "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" in on
    auto = tool_search_warning([heavy], {"ENABLE_TOOL_SEARCH": "auto:5"}, "claude-1")
    assert auto is not None and "share of context" in auto and "It was set" not in auto
    off = tool_search_warning([heavy], {"ENABLE_TOOL_SEARCH": "false"}, "claude-1")
    assert off is not None and "It was set" not in off
    assert tool_search_warning([tmp_path / "missing.jsonl"], {}, "claude-1") is None
