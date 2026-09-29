"""OpenAI Responses wire format on plain JSON: request/response dicts and SSE events.

The wire facts and their sources: docs/plans/2026-09-28-responses-format.md. Every ``Any``
here is provider JSON: untyped because the wire shape is not ours to pin.
"""

from __future__ import annotations

from typing import Any

from seatbelt.gateway.formats import as_dict, as_dicts, parse_arguments
from seatbelt.ledger.events import Event
from seatbelt.record.recorder import ModelCall, Recorder

KEEP = (
    "input",
    "instructions",
    "prompt",  # a stored prompt and its variables, which are model input
    "conversation",  # a stored conversation whose items come before the input
    "tools",
    "tool_choice",
    "max_output_tokens",
    "max_tool_calls",
    "temperature",
    "top_p",
    "reasoning",
    "text",
    "previous_response_id",
    "store",
    "include",
    "parallel_tool_calls",
    "truncation",
    "background",
)
# output items the client runs, by type: (tool name when the item has none, argument fields).
# function_call's arguments are a JSON string, parsed. Hosted tools (web_search_call and
# the like) run at the provider and have no client result, so they are not tool calls here.
_CALLS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "function_call": (None, ()),
    "custom_tool_call": (None, ("input",)),
    "local_shell_call": ("local_shell", ("action",)),
    "shell_call": ("shell", ("action",)),
    "apply_patch_call": ("apply_patch", ("operation",)),
    "computer_call": ("computer", ("action", "actions")),
    "tool_search_call": ("tool_search", ("arguments",)),  # only when the client runs it
}
# input items that answer a call, matched by call_id. Codex answers a local_shell_call with a
# function_call_output, the Agents SDK with a local_shell_call_output, which the spec's type
# keys by `id`
_RESULTS = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "local_shell_call_output",
        "shell_call_output",
        "apply_patch_call_output",
        "computer_call_output",
        "tool_search_output",
    }
)
_MCP = "mcp__"  # the namespace prefix Codex gives each MCP server's tools
_TERMINAL = frozenset({"response.completed", "response.incomplete", "response.failed"})
_SNAPSHOT = frozenset({"response.created", "response.in_progress", "response.queued"})
_FINISHED = frozenset({"completed", "incomplete"})  # statuses that are not errors


def _name(item: dict[str, Any]) -> str | None:
    """An MCP tool (namespace `mcp__<server>`) is named as Codex's hooks and Claude Code name
    it, `mcp__<server>__<tool>`, so it cannot pass for a built-in tool of the same bare name,
    in the ledger or in `tools_denied`. Any other namespace keeps the bare name, as Codex's
    hooks do: Codex's own namespaced tools (`spawn_agent` in `multi_agent_v1`), and the
    namespace equal to the name the API gives a deferred top-level tool."""
    fixed, _ = _CALLS[str(item.get("type"))]
    name = fixed or item.get("name")
    if not name:
        return None
    namespace = item.get("namespace")
    if isinstance(namespace, str) and namespace.startswith(_MCP):
        return f"{namespace.rstrip('_')}__{str(name).lstrip('_')}"
    return str(name)


def _call_id(item: dict[str, Any]) -> str | None:
    call_id = item.get("call_id")
    if not call_id and item.get("type") == "local_shell_call_output":
        call_id = item.get("id")  # the spec's form of this item
    return str(call_id) if call_id else None


def _type(item: dict[str, Any]) -> str | None:
    """The item's type; None when it is not a string, which could not be looked up."""
    kind = item.get("type")
    return kind if isinstance(kind, str) else None


def _is_call(item: dict[str, Any]) -> bool:
    """A tool call the client runs and answers. A tool search can run at either end."""
    if _type(item) == "tool_search_call" and item.get("execution") != "client":
        return False
    return _type(item) in _CALLS and bool(item.get("call_id"))


def _is_result(item: dict[str, Any]) -> bool:
    return _type(item) in _RESULTS


def _arguments(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("type") == "function_call":
        return parse_arguments(item.get("arguments"))
    _, fields = _CALLS[str(item.get("type"))]
    return {f: item[f] for f in fields if f in item}


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    return as_dicts(body.get("input"))  # a plain string input carries no items


class OpenAIResponsesFormat:
    provider = "openai"

    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self._open: dict[str, Event] = {}  # call_id -> tool.call event

    @staticmethod
    def model(body: dict[str, Any]) -> str:
        return str(body.get("model") or "unknown")

    def begin(self, body: dict[str, Any]) -> ModelCall:
        """Record tool results carried in the input, then the request. Clients that resend
        the whole history each turn (Codex) repeat old results; each is recorded once."""
        for item in _items(body):
            call_id = _call_id(item)
            if not _is_result(item) or call_id is None:
                continue
            call = self._open.pop(call_id, None)
            if call is not None:
                failed = item.get("status") == "failed"  # apply_patch_call_output reports it
                result = item.get("output") if "output" in item else item.get("tools")
                self._rec.tool_returned(call, result, "tool reported failure" if failed else None)
        request = {k: body[k] for k in KEEP if k in body}
        return self._rec.model_requested(self.model(body), request, provider=self.provider)

    @staticmethod
    def tool_result_calls(body: dict[str, Any]) -> list[tuple[str, str | None]]:
        """(call_id, tool name the input gives it) for every tool result in the request."""
        items = _items(body)
        names = {str(i["call_id"]): _name(i) for i in items if _is_call(i)}
        return [
            (call_id, names.get(call_id))
            for i in items
            if _is_result(i) and (call_id := _call_id(i)) is not None
        ]

    @staticmethod
    def unrecordable(body: dict[str, Any]) -> str | None:
        if body.get("background") is True:
            # its output is fetched later with GET /v1/responses/{id}, which is not recorded
            return "background responses are not recorded"
        return None

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]:
        """Record the response and any tool calls it asks for; returns the tool.call events."""
        if response is None:
            call.respond({}, error=error)
            return []
        status = response.get("status")
        if not isinstance(status, str):
            status = None
        if error is None and status not in _FINISHED:
            error = _status_error(response)
        model = response.get("model")
        answer = call.respond(
            response,
            _usage(as_dict(response.get("usage"))),
            response_model=model if isinstance(model, str) else None,
            error=error,
        )
        calls: list[Event] = []
        for item in as_dicts(response.get("output")):
            if not _is_call(item):
                continue
            # an item cut off mid-stream may carry truncated arguments
            done = item.get("status", "completed" if status == "completed" else None)
            name = _name(item)
            if done != "completed" or name is None:
                continue
            event = self._rec.tool_called(
                name, _arguments(item), call_id=str(item["call_id"]), parent_id=answer.id
            )
            self._open[str(item["call_id"])] = event
            calls.append(event)
        return calls


def _status_error(response: dict[str, Any]) -> str:
    status = response.get("status") if isinstance(response.get("status"), str) else None
    status = status or "unknown"
    err = as_dict(response.get("error"))
    if err:
        return f"response {status}: {err.get('code')}: {err.get('message')}"
    return f"response did not complete (status {status})"


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    """`input_tokens_details.cached_tokens` -> `input_tokens.cached_tokens`, and so on."""
    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int):
            out[key] = value
        elif key.endswith("_details"):
            for leaf, n in as_dict(value).items():
                if isinstance(n, int):
                    out[f"{key[:-8]}.{leaf}"] = n
    return out


_PARTIAL = {  # delta event -> the field of an unfinished item it fills
    "response.output_text.delta": "text",
    "response.function_call_arguments.delta": "arguments",
    "response.custom_tool_call_input.delta": "input",
}


def assemble_sse(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild the response from the stream's events, as far as they got. Never raises: the
    gateway calls this in a `finally`, and a malformed stream must still be recorded.

    A terminal event carries the whole response, recorded as sent; its `output` is filled from
    the finished items if it came without one. With no terminal event the response is rebuilt:
    the last snapshot, the items finished so far, and the unfinished ones as they started with
    their text, arguments or input so far. In a rebuilt response an item that arrived whole
    (`output_item.done`) is marked completed even where its type has no status
    (`custom_tool_call`), and an unfinished one in_progress, so `finish` records the one as a
    tool call and not the other."""
    snapshot: dict[str, Any] = {}
    terminal: dict[str, Any] | None = None
    started: dict[int, dict[str, Any]] = {}
    finished: dict[int, dict[str, Any]] = {}
    partial: dict[tuple[int, str], str] = {}
    error: dict[str, Any] | None = None
    for event in events:
        kind = event.get("type")
        index = event.get("output_index")
        if not isinstance(kind, str):
            continue
        if kind in _TERMINAL:
            terminal = as_dict(event.get("response"))
        elif kind in _SNAPSHOT:
            snapshot = as_dict(event.get("response")) or snapshot
        elif kind == "error":  # flat in the spec; nested under "error" in Codex's fixtures
            error = as_dict(event.get("error")) or {
                k: event[k] for k in ("code", "message", "param") if k in event
            }
        elif not isinstance(index, int):
            continue
        elif kind == "response.output_item.added":
            started[index] = as_dict(event.get("item"))
        elif kind == "response.output_item.done":
            finished[index] = as_dict(event.get("item"))
        elif kind in _PARTIAL and isinstance(event.get("delta"), str):
            key = (index, _PARTIAL[kind])
            partial[key] = partial.get(key, "") + event["delta"]
    if terminal is not None:
        out = dict(terminal)
        if not as_dicts(out.get("output")) and finished:
            out["output"] = [finished[i] for i in sorted(finished)]
        return out
    out = {"object": "response", **snapshot}
    output: list[dict[str, Any]] = []
    for i in sorted(started.keys() | finished.keys()):
        if i in finished:
            output.append({"status": "completed", **finished[i]})
            continue
        item = {**started[i], "status": "in_progress"}
        if item.get("type") == "message" and (i, "text") in partial:
            item["content"] = [{"type": "output_text", "text": partial[(i, "text")]}]
        for field in ("arguments", "input"):
            if (i, field) in partial:
                item[field] = partial[(i, field)]
        output.append(item)
    out["output"] = output
    if error is not None:
        out["status"] = "failed"
        out["error"] = error
    return out
