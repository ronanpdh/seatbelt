"""Redaction runs before anything is written, so what is provable is what is stored."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

# Deliberately conservative: better to redact a false positive than leak a key.
PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "bearer": re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
}


def redact_text(text: str) -> str:
    for name, pattern in PATTERNS.items():
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return text


def redact(value: Any) -> Any:
    """Recursively redact strings inside dicts, lists, tuples and Pydantic models.

    Models (e.g. SDK content blocks passed back as history) become plain JSON first,
    so their strings are redacted and the event can be hashed.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return value
