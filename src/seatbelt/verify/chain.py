"""Verification: is this ledger the one that was written, in this order, complete?"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from seatbelt.ledger.events import GENESIS_HASH, Event
from seatbelt.ledger.store import Ledger


@dataclass(frozen=True)
class Verdict:
    ok: bool
    events: int
    first_bad_seq: int | None = None
    reason: str | None = None


def verify_file(path: Path) -> Verdict:
    ledger = Ledger(path, run_id=path.stem)
    return verify_events(list(ledger.read()))


def verify_events(events: list[Event]) -> Verdict:
    prev = GENESIS_HASH
    for expected_seq, event in enumerate(events):
        if event.seq != expected_seq:
            return Verdict(False, len(events), event.seq, "sequence gap or reorder")
        if event.prev_hash != prev:
            return Verdict(False, len(events), event.seq, "prev_hash does not match previous event")
        if event.compute_hash() != event.hash:
            return Verdict(False, len(events), event.seq, "content does not match its hash")
        prev = event.hash
    return Verdict(True, len(events))
