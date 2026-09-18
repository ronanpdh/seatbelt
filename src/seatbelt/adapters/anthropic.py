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

from seatbelt.gateway.formats.anthropic import AnthropicFormat
from seatbelt.record.recorder import Recorder, describe_exception

if TYPE_CHECKING:
    from anthropic import Anthropic, AsyncAnthropic
    from anthropic.lib.streaming import AsyncMessageStream, MessageStream
    from anthropic.resources.beta import AsyncBeta, Beta
    from anthropic.types import Message
    from anthropic.types.beta import BetaMessage


class _Recording:
    def __init__(self, rec: Recorder) -> None:
        self._fmt = AnthropicFormat(rec)

    @contextmanager
    def _turn(self, kwargs: dict[str, Any]) -> Generator[list[Message | BetaMessage | None]]:
        """Record tool results and the request; the caller appends the message it got, if any."""
        if kwargs.get("stream"):
            raise TypeError("create(stream=True) is not recorded; use .stream() instead")
        call = self._fmt.begin(kwargs)
        final: list[Message | BetaMessage | None] = []
        try:
            yield final
        except BaseException as exc:
            if call.response is None:
                self._fmt.finish(call, None, error=describe_exception(exc))
            raise
        response = final[0] if final else None
        if response is None:
            return
        self._fmt.finish(call, response.model_dump(mode="json"))


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
