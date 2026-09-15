"""Verification: is this ledger the one that was written, in this order, complete?"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from seatbelt.ledger.events import GENESIS_HASH, Event
from seatbelt.ledger.store import Ledger, LedgerError


@dataclass(frozen=True)
class Verdict:
    ok: bool
    events: int
    first_bad_seq: int | None = None
    reason: str | None = None


def verify_file(path: Path) -> Verdict:
    try:
        events = list(Ledger(path, run_id=path.stem).read())
    except (OSError, LedgerError) as exc:
        return Verdict(False, 0, None, str(exc))
    return verify_events(events)


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
