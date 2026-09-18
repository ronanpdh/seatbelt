"""Anthropic Messages wire format on plain JSON: request/response dicts and SSE events.

Every ``Any`` here is provider JSON: untyped because the wire shape is not ours to pin.
"""

from __future__ import annotations

import json
from typing import Any, cast

from seatbelt.ledger.events import Event
from seatbelt.record.recorder import ModelCall, Recorder

KEEP = (
    "messages",
    "system",
    "tools",
    "tool_choice",
    "max_tokens",
    "temperature",
    "thinking",
    "output_config",
    "stop_sequences",
    "betas",
)


class AnthropicFormat:
    provider = "anthropic"

    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self._open: dict[str, Event] = {}  # tool_use id -> tool.call event

    @staticmethod
    def model(body: dict[str, Any]) -> str:
        return str(body.get("model") or "unknown")

    def begin(self, body: dict[str, Any]) -> ModelCall:
        """Record tool results carried in the history, then the request."""
        for tool_use_id, content, is_error in tool_results(body):
            call = self._open.pop(tool_use_id, None)
            if call is not None:
                self._rec.tool_returned(call, content, "tool reported error" if is_error else None)
        request = {k: body[k] for k in KEEP if k in body}
        return self._rec.model_requested(self.model(body), request, provider=self.provider)

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]:
        """Record the response and any tool calls it asks for; returns the tool.call events."""
        if response is None:
            call.respond({}, error=error)
            return []
        usage = _dict(response.get("usage"))
        model = response.get("model")
        answer = call.respond(
            response,
            {k: v for k, v in usage.items() if isinstance(v, int)},
            response_model=model if isinstance(model, str) else None,
            error=error,
        )
        if response.get("stop_reason") is None:
            return []  # abandoned stream: tool inputs may be truncated
        calls: list[Event] = []
        for block in _blocks(response.get("content")):
            if block.get("type") != "tool_use" or not block.get("id") or not block.get("name"):
                continue
            event = self._rec.tool_called(
                str(block["name"]),
                _dict(block.get("input")),
                call_id=str(block["id"]),
                parent_id=answer.id,
            )
            self._open[str(block["id"])] = event
            calls.append(event)
        return calls


def _dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _blocks(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [cast(dict[str, Any], b) for b in cast(list[Any], content) if isinstance(b, dict)]


def tool_results(body: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    """(tool_use_id, content, is_error) for every tool_result block in the request history."""
    out: list[tuple[str, Any, bool]] = []
    for message in _blocks(body.get("messages")):
        for b in _blocks(message.get("content")):
            if b.get("type") == "tool_result":
                out.append((str(b.get("tool_use_id")), b.get("content"), bool(b.get("is_error"))))
    return out


def assemble_sse(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild the final message from the stream's events, as far as they got. Never raises:
    the gateway calls this in a `finally`, and a malformed stream must still be recorded."""
    message: dict[str, Any] = {"content": [], "stop_reason": None}
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, str] = {}
    for ev in events:
        i = ev.get("index", 0)
        if not isinstance(i, int):
            continue  # malformed proxy output
        match ev.get("type"):
            case "message_start":
                message = {"stop_reason": None, **_dict(ev.get("message")), "content": []}
            case "content_block_start":
                blocks[i] = dict(_dict(ev.get("content_block")))
            case "content_block_delta":
                block = blocks.setdefault(i, {})
                delta = _dict(ev.get("delta"))
                if delta.get("type") == "text_delta":
                    block["text"] = str(block.get("text", "")) + str(delta.get("text", ""))
                elif delta.get("type") == "input_json_delta":
                    partial[i] = partial.get(i, "") + str(delta.get("partial_json", ""))
            case "content_block_stop":
                if i in partial:
                    try:
                        blocks.setdefault(i, {})["input"] = json.loads(partial.pop(i) or "{}")
                    except ValueError:  # truncated stream
                        blocks.setdefault(i, {})["input"] = {}
            case "message_delta":
                message.update(_dict(ev.get("delta")))
                message["usage"] = {**_dict(message.get("usage")), **_dict(ev.get("usage"))}
            case _:
                pass
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message
