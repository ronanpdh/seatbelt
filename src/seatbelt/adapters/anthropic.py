"""Anthropic Messages API adapter.

Usage::

    client = anthropic.Anthropic()
    messages = AnthropicAdapter(rec).messages(client)
    response = messages.create(model=..., messages=..., tools=...)

The application calls ``messages.create`` exactly as it would call
``client.messages.create``. Tool calls the model asks for are recorded when the
response arrives; the matching tool results are recorded when the application
sends them back in the next request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from seatbelt.ledger.events import Event
from seatbelt.record.recorder import Recorder

if TYPE_CHECKING:
    from anthropic import Anthropic
    from anthropic.types import Message


class RecordedMessages:
    """Drop-in for ``client.messages`` that records every ``create``."""

    def __init__(self, client: Anthropic, rec: Recorder) -> None:
        self._client = client
        self._rec = rec
        self._open_calls: dict[str, Event] = {}  # tool_use id -> tool.call event

    def create(self, **kwargs: Any) -> Message:
        self._record_tool_results(kwargs.get("messages", []))
        model = str(kwargs.get("model", "unknown"))
        with self._rec.model_call(model, _request_attrs(kwargs), provider="anthropic") as call:
            # The SDK's create() is heavily overloaded (streaming variants); with
            # **kwargs pyright cannot pick one, so we state the non-streaming type.
            response = cast("Message", self._client.messages.create(**kwargs))
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
            answer = call.respond(
                response.model_dump(mode="json"), usage, response_model=response.model
            )
        for block in response.content:
            if block.type == "tool_use":
                arguments = cast(dict[str, Any], block.input)  # SDK types input as a dict
                self._open_calls[block.id] = self._rec.tool_called(
                    block.name, arguments, call_id=block.id, parent_id=answer.id
                )
        return response

    def _record_tool_results(self, messages: list[Any]) -> None:
        for raw_message in messages:
            if not isinstance(raw_message, dict):
                continue
            content = cast(dict[str, Any], raw_message).get("content")
            if not isinstance(content, list):
                continue
            for raw_block in cast(list[Any], content):
                if not isinstance(raw_block, dict):
                    continue
                block = cast(dict[str, Any], raw_block)
                if block.get("type") != "tool_result":
                    continue
                call = self._open_calls.pop(str(block.get("tool_use_id")), None)
                if call is None:
                    continue
                error = "tool reported error" if block.get("is_error") else None
                self._rec.tool_returned(call, block.get("content"), error)


class AnthropicAdapter:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    def attach(self, rec: Recorder) -> None:
        self._rec = rec

    def messages(self, client: Anthropic) -> RecordedMessages:
        return RecordedMessages(client, self._rec)


def _request_attrs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep the parts of the request that matter for reconstruction."""
    keep = ("messages", "system", "tools", "max_tokens", "temperature", "tool_choice")
    return {k: kwargs[k] for k in keep if k in kwargs}
