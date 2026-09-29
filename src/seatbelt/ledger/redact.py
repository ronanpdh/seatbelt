"""Redaction runs before anything is written, so what is provable is what is stored."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

# Redaction is irreversible once hashed, so each pattern needs a token shape, not just a
# prefix: `sk-` may not follow a letter (desk-...), and a bearer token is 16+ characters.
# A pattern with a `keep` group keeps that part (a URL's scheme, a variable's name) and
# redacts the rest of the match. A private key comes first: a key it holds is part of it.
PATTERNS: dict[str, re.Pattern[str]] = {
    # PEM, OpenSSH and PGP: the header lines (Proc-Type: ...), then the base64, also with
    # JSON's escaped newlines (a service account file), then the END line if it came
    "private_key": re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----\s*"
        r"(?:[A-Za-z][A-Za-z0-9\-]*:[^\n]*\n)*[A-Za-z0-9+/=\s\\]*"
        r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)?"
    ),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai_key": re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_\-]{20,}"),
    "aws_access_key": re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    "bearer": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    "seatbelt_key": re.compile(r"sbk_[A-Za-z0-9_\-]{43}"),  # secrets.token_urlsafe(32)
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "jwt": re.compile(
        r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"
    ),
    "stripe_key": re.compile(r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    "slack_token": re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"),
    # scheme://user:password@host keeps the scheme and the host
    "url_credentials": re.compile(r"(?P<keep>://)[^/\s:@]+:[^/\s@]+(?=@)"),
    # an env or .env line: an upper-case name with a KEY, TOKEN, SECRET or PASSWORD word, `=`
    # with no space, and a value of 16+ characters that is not all digits, a `$` reference or
    # already redacted. Code (`token = getTokenFromEnv()`, `MAX_TOKENS=4096`) is left alone
    "env_secret": re.compile(
        r"(?P<keep>(?<![A-Za-z0-9_])(?:[A-Z0-9]+_){0,8}[A-Z0-9]*(?:KEY|TOKEN|SECRET|PASSWORD)"
        r"(?:_[A-Z0-9]+){0,8}=['\"]?)"
        r"(?![0-9]+(?![^\s'\"]))(?!\$)(?!\[REDACTED:)[^\s'\"]{16,}"
    ),
}


def redact_text(text: str) -> str:
    for name, pattern in PATTERNS.items():
        keep = r"\g<keep>" if "keep" in pattern.groupindex else ""
        text = pattern.sub(f"{keep}[REDACTED:{name}]", text)
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
