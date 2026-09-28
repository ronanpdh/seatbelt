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
}
# input items that answer a call, matched by call_id (Codex answers a local_shell_call with
# a function_call_output)
_RESULTS = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "shell_call_output",
        "apply_patch_call_output",
        "computer_call_output",
    }
)
_TERMINAL = frozenset({"response.completed", "response.incomplete", "response.failed"})
_SNAPSHOT = frozenset({"response.created", "response.in_progress", "response.queued"})
_FINISHED = frozenset({"completed", "incomplete"})  # statuses that are not errors


def _name(item: dict[str, Any]) -> str | None:
    fixed, _ = _CALLS[str(item.get("type"))]
    name = fixed or item.get("name")
    return str(name) if name else None


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
            if item.get("type") not in _RESULTS:
                continue
            call = self._open.pop(str(item.get("call_id")), None)
            if call is not None:
                failed = item.get("status") == "failed"  # apply_patch_call_output reports it
                self._rec.tool_returned(
                    call, item.get("output"), "tool reported failure" if failed else None
                )
        request = {k: body[k] for k in KEEP if k in body}
        return self._rec.model_requested(self.model(body), request, provider=self.provider)

    @staticmethod
    def tool_result_calls(body: dict[str, Any]) -> list[tuple[str, str | None]]:
        """(call_id, tool name the input gives it) for every tool result in the request."""
        items = _items(body)
        names = {
            str(i.get("call_id")): _name(i)
            for i in items
            if i.get("type") in _CALLS and i.get("call_id")
        }
        return [
            (str(i.get("call_id")), names.get(str(i.get("call_id"))))
            for i in items
            if i.get("type") in _RESULTS
        ]

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]:
        """Record the response and any tool calls it asks for; returns the tool.call events."""
        if response is None:
            call.respond({}, error=error)
            return []
        status = response.get("status")
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
            if item.get("type") not in _CALLS or not item.get("call_id"):
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
    status = response.get("status") or "unknown"
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


def assemble_sse(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild the response from the stream's events, as far as they got. Never raises: the
    gateway calls this in a `finally`, and a malformed stream must still be recorded.

    A terminal event carries the whole response; its `output` is filled from the finished
    items if it came without one. With no terminal event, the last snapshot is taken with the
    items finished so far, and the unfinished ones as they started, with their text so far."""
    snapshot: dict[str, Any] = {}
    terminal: dict[str, Any] | None = None
    started: dict[int, dict[str, Any]] = {}
    finished: dict[int, dict[str, Any]] = {}
    text: dict[int, str] = {}
    error: dict[str, Any] | None = None
    for event in events:
        kind = event.get("type")
        index = event.get("output_index")
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
        elif kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text[index] = text.get(index, "") + event["delta"]
    if terminal is not None:
        out = dict(terminal)
        if not as_dicts(out.get("output")) and finished:
            out["output"] = [finished[i] for i in sorted(finished)]
        return out
    out = {"object": "response", **snapshot}
    output: list[dict[str, Any]] = []
    for i in sorted(started.keys() | finished.keys()):
        if i in finished:
            output.append(finished[i])
            continue
        item = dict(started[i])
        if item.get("type") == "message" and i in text:
            item["content"] = [{"type": "output_text", "text": text[i]}]
        output.append(item)
    out["output"] = output
    if error is not None:
        out["status"] = "failed"
        out["error"] = error
    return out
