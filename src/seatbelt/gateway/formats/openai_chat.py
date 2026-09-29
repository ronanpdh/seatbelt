"""OpenAI Chat Completions wire format on plain JSON: request/response dicts and SSE chunks.

Every ``Any`` here is provider JSON: untyped because the wire shape is not ours to pin.
"""

from __future__ import annotations

from typing import Any

from seatbelt.gateway.formats import as_dict, as_dicts, integer, parse_arguments
from seatbelt.ledger.events import Event
from seatbelt.record.recorder import ModelCall, Recorder

KEEP = (
    "messages",
    "tools",
    "tool_choice",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "reasoning_effort",
    "stop",
    "n",
)
_USAGE = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens"}
_FUNCTION = ("function", "arguments")


class OpenAIChatFormat:
    provider = "openai"

    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self._open: dict[str, Event] = {}  # tool_call id -> tool.call event

    @staticmethod
    def model(body: dict[str, Any]) -> str:
        return str(body.get("model") or "unknown")

    def begin(self, body: dict[str, Any]) -> ModelCall:
        """Record tool results carried in the history, then the request."""
        for message in as_dicts(body.get("messages")):
            if message.get("role") == "tool":
                call = self._open.pop(str(message.get("tool_call_id")), None)
                if call is not None:
                    self._rec.tool_returned(call, message.get("content"))
        request = {k: body[k] for k in KEEP if k in body}
        return self._rec.model_requested(self.model(body), request, provider=self.provider)

    @staticmethod
    def tool_result_calls(body: dict[str, Any]) -> list[tuple[str, str | None]]:
        """(tool_call id, tool name the history gives it) for every tool message in the request."""
        messages = as_dicts(body.get("messages"))
        names = {
            str(tc.get("id")): _call(tc)[0]
            for m in messages
            for tc in as_dicts(m.get("tool_calls"))
        }
        return [
            (str(m.get("tool_call_id")), names.get(str(m.get("tool_call_id"))))
            for m in messages
            if m.get("role") == "tool"
        ]

    @staticmethod
    def unrecordable(body: dict[str, Any]) -> str | None:
        """Legacy function calling has no call ids, so its calls and results would not be
        recorded as tool calls or checked against the policy; and the record holds one choice."""
        if body.get("functions") is not None or body.get("function_call") is not None:
            return "legacy `functions`/`function_call` are not recorded; use `tools`"
        if any(m.get("role") == "function" for m in as_dicts(body.get("messages"))):
            return "legacy `function` messages are not recorded; use `tools`"
        n, count = body.get("n"), integer(body.get("n"))
        if n is not None and (count is None or count > 1):
            return f"n {n!r} is not recorded: one choice per request"
        return None

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]:
        """Record the response and any tool calls it asks for; returns the tool.call events."""
        if response is None:
            call.respond({}, error=error)
            return []
        choices = as_dicts(response.get("choices"))
        ended = bool(choices) and choices[0].get("finish_reason") is not None
        if error is None and not ended:
            err = as_dict(response.get("error"))  # an error chunk mid-stream
            error = (
                f"{err.get('type') or err.get('code')}: {err.get('message')}"
                if err
                else "response ended without a finish reason"
            )
        model = response.get("model")
        answer = call.respond(
            response,
            _usage(as_dict(response.get("usage"))),
            response_model=model if isinstance(model, str) else None,
            error=error,
        )
        if not ended:
            return []  # abandoned stream: tool arguments may be truncated
        calls: list[Event] = []
        for tc in as_dicts(as_dict(choices[0].get("message")).get("tool_calls")):
            name, arguments = _call(tc)
            if not tc.get("id") or name is None:
                continue
            event = self._rec.tool_called(
                name, arguments, call_id=str(tc["id"]), parent_id=answer.id
            )
            self._open[str(tc["id"])] = event
            calls.append(event)
        return calls


def _call(tc: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """A tool call's name and arguments. A custom tool (`type: custom`) takes free-form
    input, recorded as `{"input": ...}` as the Responses format records `custom_tool_call`."""
    if tc.get("type") == "custom":
        custom = as_dict(tc.get("custom"))
        name, arguments = custom.get("name"), {"input": custom.get("input")}
    else:
        fn = as_dict(tc.get("function"))
        name, arguments = fn.get("name"), parse_arguments(fn.get("arguments"))
    return (str(name) if name else None), arguments


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    """`prompt_tokens_details.cached_tokens` -> `input_tokens.cached_tokens`, and so on."""
    out: dict[str, int] = {}
    for key, value in raw.items():
        if key in _USAGE and isinstance(value, int):
            out[_USAGE[key]] = value
        elif key.endswith("_details") and key[:-8] in _USAGE:
            for leaf, n in as_dict(value).items():
                if isinstance(n, int):
                    out[f"{_USAGE[key[:-8]]}.{leaf}"] = n
    return out


def assemble_sse(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild the completion from the stream's chunks, as far as they got. Never raises:
    the gateway calls this in a `finally`, and a malformed stream must still be recorded."""
    out: dict[str, Any] = {"object": "chat.completion"}
    message: dict[str, Any] = {"role": "assistant", "content": None}
    tool_calls: dict[int, dict[str, Any]] = {}
    finish: str | None = None
    for chunk in chunks:
        out.update(
            {k: chunk[k] for k in ("id", "created", "model", "system_fingerprint") if k in chunk}
        )
        if as_dict(chunk.get("usage")):
            out["usage"] = chunk["usage"]
        for choice in as_dicts(chunk.get("choices")):
            delta = as_dict(choice.get("delta"))
            if isinstance(delta.get("role"), str):
                message["role"] = delta["role"]
            for field in ("content", "refusal"):
                if isinstance(delta.get(field), str):
                    message[field] = str(message.get(field) or "") + delta[field]
            for tc in as_dicts(delta.get("tool_calls")):
                i = tc.get("index", 0)
                if not isinstance(i, int):
                    continue  # malformed proxy output
                slot = tool_calls.setdefault(i, {"id": None, "type": "function"})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("type") == "custom":
                    slot["type"] = "custom"
                # a custom tool's input streams as custom.input, a function's as arguments
                kind, field = ("custom", "input") if slot["type"] == "custom" else _FUNCTION
                part = slot.setdefault(kind, {"name": "", field: ""})
                fresh = as_dict(tc.get(kind))
                if fresh.get("name"):
                    part["name"] = fresh["name"]
                part[field] = str(part.get(field) or "") + str(fresh.get(field) or "")
            if isinstance(choice.get("finish_reason"), str):
                finish = choice["finish_reason"]
        if as_dict(chunk.get("error")):  # an error chunk mid-stream; `finish` records it
            out["error"] = chunk["error"]
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    # one choice: the gateway refuses n > 1 (`unrecordable`)
    out["choices"] = [{"index": 0, "finish_reason": finish, "message": message}]
    return out
