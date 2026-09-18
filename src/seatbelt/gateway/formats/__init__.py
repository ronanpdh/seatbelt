"""Wire formats on plain JSON. `Any` here is provider JSON: the wire shape is not ours to pin."""

from __future__ import annotations

from typing import Any, cast


def as_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_dicts(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [cast(dict[str, Any], b) for b in cast(list[Any], content) if isinstance(b, dict)]
