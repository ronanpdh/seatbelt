"""Anthropic Messages wire format on plain JSON: request/response dicts and SSE events.

Every ``Any`` here is provider JSON: untyped because the wire shape is not ours to pin.
"""

from __future__ import annotations

import json
from typing import Any, cast

from seatbelt.gateway import claude_code
from seatbelt.gateway.formats import as_dict, as_dicts
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

    @staticmethod
    def tool_result_calls(body: dict[str, Any]) -> list[tuple[str, str | None]]:
        """(tool_use id, tool name the history gives it) for every tool result in the request."""
        names = {
            str(b.get("id")): str(b.get("name"))
            for m in as_dicts(body.get("messages"))
            for b in as_dicts(m.get("content"))
            if b.get("type") == "tool_use"
        }
        return [(tid, names.get(tid)) for tid, _, _ in tool_results(body)]

    @staticmethod
    def stopped_before_run(body: dict[str, Any], call_id: str, tool: str) -> bool:
        """Whether every result the request carries for `call_id` is Claude Code's report that
        seatbelt's hook stopped `tool` before it ran (`claude_code.stopped_before_run`)."""
        results = [(c, e) for tid, c, e in tool_results(body) if tid == call_id]
        return bool(results) and all(claude_code.stopped_before_run(tool, c, e) for c, e in results)

    @staticmethod
    def unrecordable(body: dict[str, Any]) -> str | None:
        return None

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]:
        """Record the response and any tool calls it asks for; returns the tool.call events."""
        if response is None:
            call.respond({}, error=error)
            return []
        if error is None and response.get("stop_reason") is None:
            error = _stream_error(response)
        usage = as_dict(response.get("usage"))
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
        for block in as_dicts(response.get("content")):
            if block.get("type") != "tool_use" or not block.get("id") or not block.get("name"):
                continue
            event = self._rec.tool_called(
                str(block["name"]),
                as_dict(block.get("input")),
                call_id=str(block["id"]),
                parent_id=answer.id,
            )
            self._open[str(block["id"])] = event
            calls.append(event)
        return calls


def _stream_error(response: dict[str, Any]) -> str:
    """A response that ended without a stop reason: the stream's error event, if it sent one."""
    err = as_dict(response.get("error"))
    if err:
        return f"{err.get('type')}: {err.get('message')}"
    return "response ended without a stop reason"


def tool_results(body: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    """(tool_use_id, content, is_error) for every tool_result block in the request history."""
    out: list[tuple[str, Any, bool]] = []
    for message in as_dicts(body.get("messages")):
        for b in as_dicts(message.get("content")):
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
                message = {"stop_reason": None, **as_dict(ev.get("message")), "content": []}
            case "content_block_start":
                blocks[i] = dict(as_dict(ev.get("content_block")))
            case "content_block_delta":
                block = blocks.setdefault(i, {})
                delta = as_dict(ev.get("delta"))
                match delta.get("type"):
                    case "text_delta":
                        block["text"] = str(block.get("text", "")) + str(delta.get("text", ""))
                    case "thinking_delta":
                        thinking = str(delta.get("thinking", ""))
                        block["thinking"] = str(block.get("thinking", "")) + thinking
                    case "signature_delta":
                        block["signature"] = str(delta.get("signature", ""))
                    case "citations_delta":
                        cited = block.get("citations")
                        block["citations"] = [
                            *(cast(list[Any], cited) if isinstance(cited, list) else []),
                            delta.get("citation"),
                        ]
                    case "input_json_delta":
                        partial[i] = partial.get(i, "") + str(delta.get("partial_json", ""))
                    case _:
                        pass
            case "content_block_stop":
                if i in partial:
                    try:
                        blocks.setdefault(i, {})["input"] = json.loads(partial.pop(i) or "{}")
                    except ValueError:  # truncated stream
                        blocks.setdefault(i, {})["input"] = {}
            case "message_delta":
                message.update(as_dict(ev.get("delta")))
                message["usage"] = {**as_dict(message.get("usage")), **as_dict(ev.get("usage"))}
            case "error":  # mid-stream, e.g. overloaded_error; `finish` records the last one
                message["error"] = as_dict(ev.get("error"))
            case _:
                pass
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message
