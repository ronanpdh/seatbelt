"""Anthropic Messages API adapter.

Usage::

    messages = AnthropicAdapter(rec).messages(client)          # or client.beta
    response = messages.create(model=..., messages=..., tools=...)
    with messages.stream(model=..., messages=...) as stream: ...

    messages = AnthropicAdapter(rec).async_messages(async_client)  # or async_client.beta

Call ``create`` and ``stream`` exactly as on ``client.messages``. Tool calls are recorded
when the response arrives; their results when the application sends them back in the
next request.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, cast

from seatbelt.ledger.events import Event
from seatbelt.record.recorder import Recorder

if TYPE_CHECKING:
    from anthropic import Anthropic, AsyncAnthropic
    from anthropic.lib.streaming import AsyncMessageStream, MessageStream
    from anthropic.resources.beta import AsyncBeta, Beta
    from anthropic.types import Message
    from anthropic.types.beta import BetaMessage

_KEEP = (
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


class _Recording:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self._open_calls: dict[str, Event] = {}  # tool_use id -> tool.call event

    @contextmanager
    def _turn(self, kwargs: dict[str, Any]) -> Generator[list[Message | BetaMessage | None]]:
        """Record tool results and the request; the caller appends the message it got, if any."""
        if kwargs.get("stream"):
            raise TypeError("create(stream=True) is not recorded; use .stream() instead")
        self._record_tool_results(kwargs.get("messages", []))
        model = str(kwargs.get("model", "unknown"))
        request = {k: kwargs[k] for k in _KEEP if k in kwargs}
        final: list[Message | BetaMessage | None] = []
        with self._rec.model_call(model, request, provider="anthropic") as call:
            yield final
            response = final[0]
            if response is None:
                return
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
            answer = call.respond(
                response.model_dump(mode="json"), usage, response_model=response.model
            )
        if response.stop_reason is None:
            return  # abandoned stream: tool inputs may be truncated
        for block in response.content:
            if block.type == "tool_use":
                arguments = cast(dict[str, Any], block.input)  # SDK types input as a dict
                self._open_calls[block.id] = self._rec.tool_called(
                    block.name, arguments, call_id=block.id, parent_id=answer.id
                )

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


class RecordedMessages(_Recording):
    """Drop-in for ``client.messages`` or ``client.beta.messages``."""

    def __init__(self, client: Anthropic | Beta, rec: Recorder) -> None:
        super().__init__(rec)
        self._client = client

    def create(self, **kwargs: Any) -> Message:
        with self._turn(kwargs) as final:
            # create() is overloaded on stream; _turn rejects stream=True, so this is a Message.
            response = cast("Message", self._client.messages.create(**kwargs))
            final.append(response)
        return response

    @contextmanager
    def stream(self, **kwargs: Any) -> Generator[MessageStream]:
        with self._turn(kwargs) as final, self._client.messages.stream(**kwargs) as stream:
            yield cast("MessageStream", stream)
            final.append(_snapshot(stream))


class AsyncRecordedMessages(_Recording):
    """Drop-in for ``async_client.messages`` or ``async_client.beta.messages``."""

    def __init__(self, client: AsyncAnthropic | AsyncBeta, rec: Recorder) -> None:
        super().__init__(rec)
        self._client = client

    async def create(self, **kwargs: Any) -> Message:
        with self._turn(kwargs) as final:
            response = cast("Message", await self._client.messages.create(**kwargs))
            final.append(response)
        return response

    @asynccontextmanager
    async def stream(self, **kwargs: Any) -> AsyncGenerator[AsyncMessageStream]:
        with self._turn(kwargs) as final:
            async with self._client.messages.stream(**kwargs) as stream:
                yield cast("AsyncMessageStream", stream)
                final.append(_snapshot(stream))


def _snapshot(stream: Any) -> Message | None:
    """What the caller received, without reading further: exits behave like the SDK's."""
    try:
        return cast("Message | None", stream.current_message_snapshot)
    except AssertionError:  # no events read yet
        return None


class AnthropicAdapter:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    def messages(self, client: Anthropic | Beta) -> RecordedMessages:
        return RecordedMessages(client, self._rec)

    def async_messages(self, client: AsyncAnthropic | AsyncBeta) -> AsyncRecordedMessages:
        return AsyncRecordedMessages(client, self._rec)
