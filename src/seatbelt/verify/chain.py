"""Verification: is this ledger the one that was written, in this order, complete?"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from seatbelt.ledger.events import GENESIS_HASH, SCHEMA_VERSION, Event, Kind
from seatbelt.ledger.store import LedgerError, read_events


@dataclass(frozen=True)
class Verdict:
    ok: bool
    events: int
    first_bad_seq: int | None = None
    reason: str | None = None
    complete: bool = False  # ends in a run.end whose event count matches


def verify_file(path: Path) -> Verdict:
    try:
        events = list(read_events(path))
    except (OSError, UnicodeDecodeError, LedgerError) as exc:
        return Verdict(False, 0, None, str(exc))
    return verify_events(events)


def verify_events(events: list[Event]) -> Verdict:
    prev = GENESIS_HASH
    for expected_seq, event in enumerate(events):
        if event.schema_version != SCHEMA_VERSION:
            reason = f"unsupported schema version {event.schema_version}"
            return Verdict(False, len(events), event.seq, reason)
        if event.seq != expected_seq:
            return Verdict(False, len(events), event.seq, "sequence gap or reorder")
        if event.prev_hash != prev:
            return Verdict(False, len(events), event.seq, "prev_hash does not match previous event")
        if event.compute_hash() != event.hash:
            return Verdict(False, len(events), event.seq, "content does not match its hash")
        prev = event.hash
    last = events[-1] if events else None
    complete = (
        last is not None
        and events[0].kind == Kind.RUN_START
        and sum(e.kind == Kind.RUN_START for e in events) == 1
        and last.kind == Kind.RUN_END
        and last.attrs.get("run.events") == len(events)
    )
    return Verdict(True, len(events), complete=complete)
