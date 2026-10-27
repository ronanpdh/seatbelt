"""Verification: is this ledger the one that was written, in this order, complete?"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from seatbelt.attest.manifest import sidecar
from seatbelt.ledger.events import GENESIS_HASH, SCHEMA_VERSION, Event, Kind
from seatbelt.ledger.store import LedgerError, parse_events


@dataclass(frozen=True)
class Verdict:
    ok: bool
    events: int
    first_bad_seq: int | None = None
    reason: str | None = None
    complete: bool = False  # ends in a run.end whose event count matches


@dataclass(frozen=True)
class Snapshot:
    """A ledger and its signature file as one check sees them: the signature file read first,
    then the ledger, each once. Whatever is shown, counted or signed after the check is made
    from `data`, the bytes that were checked, never from a second read, which could be of
    other bytes. A ledger is not written to once it is signed, so a run that closes and is
    signed while it is read is seen open and unsigned, never as a forgery."""

    data: bytes  # the ledger's
    signature: bytes | None  # the signature file's; None when there is none or it is unreadable
    unreadable: str | None = None  # why the signature file, which is there, cannot be read


def snapshot(ledger: Path) -> Snapshot:
    """Read `ledger` as a check must (see `Snapshot`). Raises OSError when the ledger itself
    cannot be read."""
    signature: bytes | None = None
    unreadable: str | None = None
    try:
        signature = sidecar(ledger).read_bytes()
    except FileNotFoundError:
        pass
    except OSError as exc:
        unreadable = str(exc)
    return Snapshot(ledger.read_bytes(), signature, unreadable)


def verify_data(data: bytes, source: object) -> tuple[list[Event], Verdict]:
    """The events in `data`, a ledger's bytes, and their verdict. No events when a line cannot
    be read. `source` names the ledger in errors."""
    try:
        events = list(parse_events(data, source))
    except LedgerError as exc:
        return [], Verdict(False, 0, None, str(exc))
    return events, verify_events(events)


def read_verified(ledger: Path) -> tuple[Snapshot | None, list[Event], Verdict]:
    """`snapshot(ledger)`, its events and their verdict, for a caller that goes on to check the
    signature (pass it the snapshot) and show, count or sign the events. No snapshot and no
    events when the ledger cannot be read."""
    try:
        snap = snapshot(ledger)
    except OSError as exc:
        return None, [], Verdict(False, 0, None, str(exc))
    events, verdict = verify_data(snap.data, ledger)
    return snap, events, verdict


def verify_file(path: Path) -> Verdict:
    return read_verified(path)[2]


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
