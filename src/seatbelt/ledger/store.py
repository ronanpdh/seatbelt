"""Append-only JSONL ledger with a SHA-256 hash chain."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from seatbelt.ledger.events import GENESIS_HASH, SCHEMA_VERSION, Actor, Event, Kind


class LedgerError(Exception):
    """Raised when the ledger cannot be trusted."""


def read_events(path: Path) -> Iterator[Event]:
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield Event.model_validate_json(line)
            except (ValidationError, UnicodeDecodeError) as exc:
                msg = f"{path}:{lineno} is not a valid event: {exc}"
                raise LedgerError(msg) from exc


class Ledger:
    """One file per run. Every append links to the previous event's hash."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._seq = 0
        self._last_hash = GENESIS_HASH
        self._lock = threading.Lock()  # seq and prev_hash must advance atomically
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
        with self._lock:
            event = Event(
                schema_version=SCHEMA_VERSION,
                run_id=self.run_id,
                seq=self._seq,
                kind=kind,
                actor=actor,
                parent_id=parent_id,
                attrs=attrs or {},
                prev_hash=self._last_hash,
            ).sealed()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(mode=0o600)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(event.model_dump_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # an audit record that can vanish on power loss is not one
            self._seq += 1
            self._last_hash = event.hash
            return event

    @property
    def last_hash(self) -> str:
        return self._last_hash

    @property
    def length(self) -> int:
        return self._seq
