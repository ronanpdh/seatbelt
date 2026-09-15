"""The adapter contract. Adapters translate a framework's events into Recorder calls."""

from __future__ import annotations

from typing import Protocol

from seatbelt.record.recorder import Recorder


class Adapter(Protocol):
    """Anything that can attach a Recorder to a framework or client.

    An adapter never interprets what it sees. It maps requests, responses,
    tool calls and tool results onto the Recorder API and nothing else.
    """

    def attach(self, rec: Recorder) -> None: ...
