"""Manifest over a finished ledger: the facts a signature pins that the writer cannot rewrite."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from seatbelt import __version__
from seatbelt.ledger.store import read_events

ATTEST_VERSION = 1


class AttestError(Exception):
    """Raised when a key or manifest cannot be used."""


class Signed(BaseModel):
    """Base for anything the Ed25519 key signs: canonical form is everything but `signature`."""

    model_config = ConfigDict(extra="forbid")

    public_key: str = ""  # informational; trust comes from the verifier's own key file
    signature: str = ""

    def canonical(self) -> bytes:
        data = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


class Manifest(Signed):
    attest_version: int
    run_id: str
    schema_version: int
    events: int
    final_hash: str
    ledger_sha256: str
    harness: str
    signed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def sidecar(ledger: Path) -> Path:
    return ledger.with_suffix(".attest.json")


def build(ledger: Path) -> Manifest:
    events = list(read_events(ledger))
    if not events:
        raise AttestError(f"{ledger} has no events")
    last = events[-1]
    return Manifest(
        attest_version=ATTEST_VERSION,
        run_id=last.run_id,
        schema_version=last.schema_version,
        events=len(events),
        final_hash=last.hash,
        ledger_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest(),
        harness=f"seatbelt {__version__}",
    )
