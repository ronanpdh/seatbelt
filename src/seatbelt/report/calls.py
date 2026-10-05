"""Each model call in a ledger as numbers: how long the conversation and its tool list were,
how big the request, and the tokens the provider counted. No prompt or tool text, so the
table can be shared where the ledger cannot, e.g. to find why a run's context filled up."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from seatbelt.gateway.formats import as_dict, as_dicts
from seatbelt.ledger.events import Event, Kind

MCP_PREFIX = "mcp__"  # Claude Code names an MCP tool mcp__<server>__<tool>
# MCP tool JSON sent in full with one request, from which seatbelt points at tool search: tens
# of thousands of tokens on every request, which tool search would have left out
INLINE_MCP_WARN = 100 * 1024


@dataclass(frozen=True)
class Call:
    """One model call. A request with no response recorded has no usage."""

    model: str
    messages: int
    tools: int
    mcp: int  # tools named mcp__...
    deferred: int  # tools sent with defer_loading: Claude Code's MCP tool search
    inline_mcp: int  # MCP tools sent in full, not deferred
    inline_mcp_bytes: int  # their JSON
    request_bytes: int  # the request as recorded, as compact JSON
    usage: dict[str, int]  # as the provider reported it, e.g. cache_read_input_tokens
    error: str | None  # also set when no response was recorded


def _compact(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def _messages(request: dict[str, Any]) -> int:
    """Turns sent: Anthropic and Chat `messages`, Responses `input`, Gemini `contents`."""
    for key in ("messages", "input", "contents"):
        value = request.get(key)
        if isinstance(value, list):
            return len(as_dicts(value))
        if isinstance(value, str):
            return 1
    return 0


def _tools(request: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per tool; a Gemini tool entry declares several functions."""
    out: list[dict[str, Any]] = []
    for tool in as_dicts(request.get("tools")):
        declared = as_dicts(tool.get("functionDeclarations"))
        out.extend(declared or [tool])
    return out


def _name(tool: dict[str, Any]) -> str:
    name = tool.get("name") or as_dict(tool.get("function")).get("name")
    return name if isinstance(name, str) else ""


def _requested(request: Event) -> Call:
    """What the request says; the response fills in usage, model and error."""
    req = as_dict(request.attrs.get("gen_ai.request"))
    tools = _tools(req)
    mcp = [t for t in tools if _name(t).startswith(MCP_PREFIX)]
    inline = [t for t in mcp if t.get("defer_loading") is not True]
    return Call(
        model=str(request.attrs.get("gen_ai.request.model") or "unknown"),
        messages=_messages(req),
        tools=len(tools),
        mcp=len(mcp),
        deferred=sum(t.get("defer_loading") is True for t in tools),
        inline_mcp=len(inline),
        inline_mcp_bytes=sum(_compact(t) for t in inline),
        request_bytes=_compact(req),
        usage={},
        error="no response recorded",
    )


def _answered(call: Call, response: Event) -> Call:
    attrs = response.attrs
    usage = {
        k.removeprefix("gen_ai.usage."): v
        for k, v in attrs.items()
        if k.startswith("gen_ai.usage.") and isinstance(v, int)
    }
    model = attrs.get("gen_ai.response.model")
    error = attrs.get("error")
    return replace(
        call,
        model=model if isinstance(model, str) and model else call.model,
        usage=usage,
        error=str(error) if error else None,
    )


def calls(events: Iterable[Event]) -> list[Call]:
    """The model calls in `events`, in the order they were made, each with its response (a
    response names its request as its parent). Only each call's numbers are kept, never its
    request, so a long run's ledger is read in one pass without holding it."""
    made: dict[str, Call] = {}
    for event in events:
        if event.kind is Kind.MODEL_REQUEST:
            made[event.id] = _requested(event)
        elif event.kind is Kind.MODEL_RESPONSE and event.parent_id in made:
            made[event.parent_id] = _answered(made[event.parent_id], event)
    return list(made.values())


def requested(events: Iterable[Event], limit: int | None = None) -> Iterator[Call]:
    """The first `limit` model requests (all by default), as they were sent, read lazily: what
    a client sends with every request shows in its first few."""
    seen = 0
    for event in events:
        if limit is not None and seen >= limit:
            return
        if event.kind is Kind.MODEL_REQUEST:
            seen += 1
            yield _requested(event)


def heaviest_inline_mcp(made: Iterable[Call]) -> Call | None:
    """The call that sent the most MCP tool JSON in full, if that reaches INLINE_MCP_WARN."""
    over = [c for c in made if c.inline_mcp_bytes >= INLINE_MCP_WARN]
    return max(over, key=lambda c: c.inline_mcp_bytes, default=None)


def size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"


def inline_mcp_note(call: Call) -> str:
    """Why so many tools went in full, and the switch for it (code.claude.com/docs/en/env-vars:
    behind a non-first-party ANTHROPIC_BASE_URL, MCP tool search is disabled by default)."""
    return (
        f"{call.inline_mcp} MCP tools were sent in full with a request "
        f"({size(call.inline_mcp_bytes)} of tool definitions). Claude Code does this when MCP "
        "tool search is off, as it is by default behind a base URL other than Anthropic's; "
        "ENABLE_TOOL_SEARCH=true turns it on."
    )
