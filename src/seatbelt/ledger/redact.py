"""Redaction runs before anything is written, so what is provable is what is stored."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

# Redaction is irreversible once hashed, so each pattern needs a token shape, not just a
# prefix: `sk-` may not follow a letter (desk-...), and a bearer token is 16+ characters.
PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai_key": re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_\-]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "bearer": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
}


def redact_text(text: str) -> str:
    for name, pattern in PATTERNS.items():
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return text


def unique_key(key: str, taken: dict[str, Any]) -> str:
    """`key`, or `key#2`, `key#3`, ... when rewriting keys made two equal: neither value is lost."""
    candidate, n = key, 1
    while candidate in taken:
        n += 1
        candidate = f"{key}#{n}"
    return candidate


def redact(value: Any) -> Any:
    """Recursively redact strings, and dict keys, inside dicts, lists, tuples and Pydantic models.

    Models (e.g. SDK content blocks passed back as history) become plain JSON first,
    so their strings are redacted and the event can be hashed.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
            out[unique_key(redact_text(str(k)), out)] = redact(v)  # pyright: ignore[reportUnknownArgumentType]
        return out
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return value
