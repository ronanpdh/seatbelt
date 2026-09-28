"""Wire formats on plain JSON. `Any` here is provider JSON: the wire shape is not ours to pin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from seatbelt.ledger.events import Event
    from seatbelt.record.recorder import ModelCall, Recorder


class Format(Protocol):
    """One wire format's recorder: requests in, responses and tool calls out."""

    provider: str

    def __init__(self, rec: Recorder) -> None: ...

    def begin(self, body: dict[str, Any]) -> ModelCall: ...

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]: ...


def as_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_dicts(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [cast(dict[str, Any], b) for b in cast(list[Any], content) if isinstance(b, dict)]
