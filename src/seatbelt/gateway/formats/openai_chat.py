"""OpenAI Chat Completions wire format on plain JSON: request/response dicts and SSE chunks.

Every ``Any`` here is provider JSON: untyped because the wire shape is not ours to pin.
"""

from __future__ import annotations

import json
from typing import Any

from seatbelt.gateway.formats import as_dict, as_dicts
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

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]:
        """Record the response and any tool calls it asks for; returns the tool.call events."""
        if response is None:
            call.respond({}, error=error)
            return []
        model = response.get("model")
        answer = call.respond(
            response,
            _usage(as_dict(response.get("usage"))),
            response_model=model if isinstance(model, str) else None,
            error=error,
        )
        choices = as_dicts(response.get("choices"))
        if not choices or choices[0].get("finish_reason") is None:
            return []  # abandoned stream: tool arguments may be truncated
        calls: list[Event] = []
        for tc in as_dicts(as_dict(choices[0].get("message")).get("tool_calls")):
            fn = as_dict(tc.get("function"))
            if not tc.get("id") or not fn.get("name"):
                continue
            event = self._rec.tool_called(
                str(fn["name"]),
                _arguments(fn.get("arguments")),
                call_id=str(tc["id"]),
                parent_id=answer.id,
            )
            self._open[str(tc["id"])] = event
            calls.append(event)
        return calls


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


def _arguments(raw: Any) -> dict[str, Any]:
    """The provider sends a JSON string; keep it raw when it does not parse to an object.
    A non-string is taken as a dict or dropped."""
    if not isinstance(raw, str):
        return as_dict(raw)
    try:
        parsed: Any = json.loads(raw or "{}")
    except ValueError:
        return {"_raw": raw}
    return as_dict(parsed) if isinstance(parsed, dict) else {"_raw": raw}


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
            if isinstance(delta.get("content"), str):
                message["content"] = str(message.get("content") or "") + delta["content"]
            for tc in as_dicts(delta.get("tool_calls")):
                i = tc.get("index", 0)
                if not isinstance(i, int):
                    continue  # malformed proxy output
                slot = tool_calls.setdefault(
                    i, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = as_dict(tc.get("function"))
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                slot["function"]["arguments"] += str(fn.get("arguments") or "")
            if isinstance(choice.get("finish_reason"), str):
                finish = choice["finish_reason"]
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    # n > 1 collapses into one choice; the request records n
    out["choices"] = [{"index": 0, "finish_reason": finish, "message": message}]
    return out
