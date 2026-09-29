"""Wire formats on plain JSON. `Any` here is provider JSON: the wire shape is not ours to pin."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from seatbelt.ledger.events import Event
    from seatbelt.record.recorder import ModelCall, Recorder


class Format(Protocol):
    """One wire format's recorder: requests in, responses and tool calls out."""

    provider: str

    def __init__(self, rec: Recorder) -> None: ...

    def begin(self, body: dict[str, Any]) -> ModelCall: ...

    @staticmethod
    def model(body: dict[str, Any]) -> str: ...

    @staticmethod
    def tool_result_calls(body: dict[str, Any]) -> list[tuple[str, str | None]]: ...

    @staticmethod
    def unrecordable(body: dict[str, Any]) -> str | None:
        """Why this request cannot be recorded faithfully, so the gateway refuses it (400)
        rather than forward what it did not read; None to record it."""
        ...

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]: ...


def as_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_dicts(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [cast(dict[str, Any], b) for b in cast(list[Any], content) if isinstance(b, dict)]


def integer(value: Any) -> int | None:
    """An integer as proto3 JSON reads one: a JSON integer, an integral float or a string of
    digits. None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?[0-9]+\s*", value):
        return int(value)
    return None


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments the provider sends as a JSON string; kept raw when they do not parse to
    an object. A non-string is taken as a dict or dropped."""
    if not isinstance(raw, str):
        return as_dict(raw)
    try:
        parsed: Any = json.loads(raw or "{}")
    except ValueError:
        return {"_raw": raw}
    return as_dict(parsed) if isinstance(parsed, dict) else {"_raw": raw}
