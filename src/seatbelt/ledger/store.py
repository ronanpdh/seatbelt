"""Append-only JSONL ledger with a SHA-256 hash chain."""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from seatbelt.ledger.events import GENESIS_HASH, SCHEMA_VERSION, Actor, Event, Kind
from seatbelt.ledger.redact import redact_text, unique_key

# json.dumps (the hash) accepts a lone UTF-16 surrogate, UTF-8 (the file) does not
_SURROGATE = re.compile("[\ud800-\udfff]")


class LedgerError(Exception):
    """Raised when the ledger cannot be trusted."""


def read_events(path: Path) -> Generator[Event]:
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield Event.model_validate_json(line)
            except (ValidationError, UnicodeDecodeError) as exc:
                msg = f"{path}:{lineno} is not a valid event: {exc}"
                raise LedgerError(msg) from exc


def fsync_dir(directory: Path) -> None:
    """Make a new name in `directory` durable: fsyncing a file does not cover its entry."""
    if os.name == "nt":  # a directory cannot be opened on Windows; NTFS journals names
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _visible(text: str) -> str:
    return _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def writable(value: Any) -> Any:
    """`value` with every lone surrogate, in strings and dict keys, spelled as its escape
    (the text `\\ud800`), so an event that can be hashed can also be written."""
    if isinstance(value, str):
        return _visible(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in cast("dict[Any, Any]", value).items():
            out[unique_key(_visible(k), out) if isinstance(k, str) else k] = writable(v)
        return out
    if isinstance(value, list | tuple):
        return [writable(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _clean(text: str) -> str:
    return redact_text(_visible(text))


class Ledger:
    """One file per run. Every append links to the previous event's hash."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._seq = 0
        self._last_hash = GENESIS_HASH
        self._lock = threading.Lock()  # seq and prev_hash must advance atomically
        self._torn = False  # a failed append could not be undone; the file ends mid-line
        if path.exists():
            for event in read_events(path):
                self._seq = event.seq + 1
                self._last_hash = event.hash

    def append(
        self,
        kind: Kind,
        actor: Actor,
        attrs: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> Event:
        # Redaction of actor and parent here, not only in the recorder, so no caller skips it
        actor = actor.model_copy(
            update={
                "id": _clean(actor.id),
                "version": None if actor.version is None else _clean(actor.version),
            }
        )
        with self._lock:
            if self._torn:
                raise LedgerError(f"{self.path}: an earlier append could not be undone")
            event = Event(
                schema_version=SCHEMA_VERSION,
                run_id=self.run_id,
                seq=self._seq,
                kind=kind,
                actor=actor,
                parent_id=None if parent_id is None else _clean(parent_id),
                attrs=writable(attrs or {}),
                prev_hash=self._last_hash,
            ).sealed()
            line = (event.model_dump_json() + "\n").encode()  # before the file is touched
            self.path.parent.mkdir(parents=True, exist_ok=True)
            created = not self.path.exists()
            self.path.touch(mode=0o600)
            if created:
                fsync_dir(self.path.parent)
            self._write(line)
            self._seq += 1
            self._last_hash = event.hash
            return event

    def _write(self, line: bytes) -> None:
        """Append `line` whole, or leave the file as it was: a partial line would fuse with
        the next append and make the rest of the ledger unreadable."""
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        try:
            size = os.fstat(fd).st_size
            try:
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)  # an audit record that can vanish on power loss is not one
            except BaseException:
                try:
                    os.ftruncate(fd, size)
                except OSError:
                    self._torn = True
                raise
        finally:
            os.close(fd)

    @property
    def last_hash(self) -> str:
        return self._last_hash

    @property
    def length(self) -> int:
        return self._seq
