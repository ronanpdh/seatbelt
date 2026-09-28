"""Gateway configuration: one YAML file, unknown keys rejected, employee keys stored as hashes."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import date
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

KEY_PREFIX = "sbk_"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Upstream(_Strict):
    url: str
    key_env: str  # name of the env var holding the real provider key; never the key itself


def _none_is_empty(value: object) -> object:
    return [] if value is None else value  # `key:` with its entries deleted is null in YAML


class PolicyConfig(_Strict):
    models: list[str] | None = None
    tools_denied: list[str] = Field(default_factory=list[str])
    max_output_tokens: int | None = Field(default=None, gt=0)

    _tools_denied = field_validator("tools_denied", mode="before")(_none_is_empty)


class Principal(_Strict):
    id: str
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued: date


class GatewayConfig(_Strict):
    listen: str = "127.0.0.1:8080"
    signing_key: Path | None = None  # or SEATBELT_SIGNING_KEY in the environment
    ledgers: Path
    session_idle: int = Field(default=900, gt=0)  # seconds
    upstreams: dict[str, Upstream]
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    principals: list[Principal] = Field(default_factory=list[Principal])

    _principals = field_validator("principals", mode="before")(_none_is_empty)

    def lookup(self, key: str) -> Principal | None:
        if not key:
            return None
        digest = key_hash(key)
        return next(
            (p for p in self.principals if secrets.compare_digest(p.key_sha256, digest)), None
        )


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _read(path: Path, text: str | None = None) -> dict[str, Any]:  # YAML is untyped
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8") if text is None else text)
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return cast(dict[str, Any], data)


def load_config(path: Path, text: str | None = None) -> GatewayConfig:
    """`text`, when given, is the file's content already read, so a caller that hashed it
    loads exactly what it hashed."""
    try:
        cfg = GatewayConfig.model_validate(_read(path, text))
    except ValidationError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    base = path.resolve().parent  # paths in the file are relative to the file
    if cfg.signing_key is not None:
        cfg.signing_key = base / cfg.signing_key.expanduser()
    cfg.ledgers = base / cfg.ledgers.expanduser()
    return cfg


def read_config(path: Path) -> tuple[GatewayConfig, bytes]:
    """The config and the exact bytes it came from, for a watcher to compare the file with."""
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return load_config(path, text), data


def add_principal(path: Path, principal_id: str) -> str:
    """Append a principal and return its one-time key. Rewrites the file; comments are lost."""
    path = path.resolve()  # rewrite the real file, not a symlink to it
    raw = _read(path)
    try:
        existing = GatewayConfig.model_validate(raw).principals  # never rewrite a broken file
    except ValidationError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if any(p.id == principal_id for p in existing):
        raise ValueError(f"{path}: {principal_id} already has a key; delete its line to reissue")
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    entry = {"id": principal_id, "key_sha256": key_hash(key), "issued": date.today()}
    # absent, or `principals:` with every entry deleted (null in YAML)
    principals: list[Any] = raw.get("principals") or []
    raw["principals"] = [*principals, entry]
    tmp = path.with_name(path.name + ".tmp")  # concurrent keygen runs unsupported: last writer wins
    try:
        tmp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except OSError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return key
