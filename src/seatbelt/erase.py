"""`seatbelt erase`: remove every ledger recorded under a person's principal ids.

Ledgers are hash-chained and signed, so a person's events cannot be cut out of one: whole
ledgers are removed, each named by its `run.start` `principal.id`. The removal happens inside
a signed record, `erasure-<time>-<hex>.jsonl`, which holds only SHA-256 hashes, the case
reference, who ran it and counts: never a run id, file name or object key, since a gateway run
id begins with the person's id. Nothing may be writing to a folder while it is changed, so
each folder's lock is held throughout. Design: docs/plans/2026-09-29-erasure-design.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer
from seatbelt.gateway.formats import as_dict
from seatbelt.gateway.sessions import close_open_chains
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import LedgerError, read_events
from seatbelt.locks import RUNNING, try_lock
from seatbelt.record.recorder import Recorder

SHIPPED = ".shipped"
STATE = ".state.json"  # the importer's
ERASED = ".erased"  # the importer's suppression list: sha256 of each erased principal id
PRINCIPAL = "seatbelt:erasure"
PREFIX = "erasure-"


def id_hash(principal: str) -> str:
    return hashlib.sha256(principal.encode()).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_record(events: list[Event]) -> bool:
    return bool(events) and events[0].attrs.get("principal.id") == PRINCIPAL


@dataclass
class Target:
    """One ledger to remove, with what goes with it."""

    ledger: Path
    ledger_sha256: str
    sidecar: Path | None
    sidecar_sha256: str | None
    mark: Path | None
    mark_sha256: str | None
    object_keys: list[str]
    leftover: bool = False  # already named by an earlier, interrupted erasure


@dataclass
class Plan:
    folder: Path
    targets: list[Target] = field(default_factory=list[Target])
    orphans: list[Path] = field(default_factory=list[Path])  # sidecars and marks left over
    state_entries: list[str] = field(default_factory=list[str])  # importer conversations
    unmatched: list[str] = field(default_factory=list[str])  # ledgers under unknown or none
    unreadable: list[str] = field(default_factory=list[str])
    # unreadable, and its first line names the person or cannot be read: left in place, for
    # the operator to deal with
    to_check: list[str] = field(default_factory=list[str])
    importer: bool = False  # the folder is an importer's: its suppression list is kept

    @property
    def empty(self) -> bool:
        return not (self.targets or self.orphans or self.state_entries)


@dataclass
class Result:
    folder: Path
    record: Path | None
    ledgers: int = 0
    sidecars: int = 0
    marks: int = 0
    state_entries: int = 0
    closed: list[str] = field(default_factory=list[str])  # interrupted records closed
    to_check: list[str] = field(default_factory=list[str])  # as in Plan: not removed


# -- locks ----------------------------------------------------------------------------------


def live_local_runs(folder: Path) -> list[str]:
    """The local runs (`seatbelt run`) recording into `folder` now: their lock is held."""
    live: list[str] = []
    for path in sorted((folder / RUNNING).glob("*.lock")):
        try:
            handle: IO[bytes] = path.open("rb")
        except OSError:
            continue
        with handle:
            if not try_lock(handle):
                live.append(path.stem)
    return live


# -- finding --------------------------------------------------------------------------------


def erased_hashes(folder: Path) -> set[str]:
    """Every hash named by an erasure record in the folder: what must not be left behind."""
    hashes: set[str] = set()
    for path in folder.glob(f"{PREFIX}*.jsonl"):
        try:
            events = list(read_events(path))
        except (OSError, LedgerError):
            continue
        if not is_record(events):
            continue
        for e in events:
            if e.kind is Kind.ACTION:
                for key in (
                    "erasure.ledger_sha256",
                    "erasure.sidecar_sha256",
                    "erasure.mark_sha256",
                ):
                    value = e.attrs.get(key)
                    if isinstance(value, str):
                        hashes.add(value)
    return hashes


def erased_sidecar(side: Path, erased: set[str]) -> bool:
    """A sidecar, or its ledger, that an erasure record names: left by an interrupted erase."""
    return _sha256(side) in erased or _side_ledger(side) in erased


def _first_principal(path: Path) -> str | None:
    """An unreadable ledger's run.start principal.id, read from the lines before the bad one:
    a crash tears only the last. Raises LedgerError when its first line cannot be read."""
    try:
        first = next(read_events(path), None)
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(str(exc)) from exc
    principal = first.attrs.get("principal.id") if first is not None else None
    return principal if isinstance(principal, str) else None


def _owner(meta: dict[str, Any]) -> str | None:
    owner = as_dict(meta.get("user")) or as_dict(meta.get("started_by_user"))
    value = owner.get("id")
    return value if isinstance(value, str) else None


def plan(folder: Path, principals: Iterable[str]) -> Plan:
    wanted = set(principals)
    out = Plan(folder=folder, importer=(folder / STATE).exists() or (folder / ERASED).exists())
    erased = erased_hashes(folder)
    for path in sorted(folder.glob("*.jsonl")):
        if path.name.startswith(PREFIX):
            continue
        try:
            events = list(read_events(path))
        except (OSError, UnicodeDecodeError, LedgerError):
            out.unreadable.append(path.name)
            try:
                theirs = _first_principal(path) in wanted
            except LedgerError:
                theirs = True  # whose it is cannot be told
            if theirs:
                out.to_check.append(path.name)
            continue
        if not events:
            continue
        principal = events[0].attrs.get("principal.id")
        digest = _sha256(path)
        leftover = digest in erased
        if principal in wanted or leftover:
            out.targets.append(_target(folder, path, digest, leftover))
            if "compliance.id" in events[0].attrs:
                out.importer = True
        elif principal in (None, "unknown"):
            out.unmatched.append(path.name)
    # sidecars and marks whose ledger is gone and whose hash an erasure record names
    for side in sorted(folder.glob("*.attest.json")):
        ledger = side.with_name(side.name.removesuffix(".attest.json") + ".jsonl")
        if not ledger.exists() and erased_sidecar(side, erased):
            out.orphans.append(side)
    for mark in sorted((folder / SHIPPED).glob("*")):
        if (folder / f"{mark.name}.jsonl").exists():
            continue
        if _sha256(mark) in erased or erased.intersection(_mark_hashes(mark)):
            out.orphans.append(mark)
    state = _read_state(folder)
    for key, raw in as_dict(state.get("conversations")).items():
        if _owner(as_dict(as_dict(raw).get("meta"))) in wanted:
            out.state_entries.append(key)
    return out


def _side_ledger(side: Path) -> str | None:
    try:
        value = json.loads(side.read_text()).get("ledger_sha256")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _mark_hashes(mark: Path) -> set[str]:
    try:
        data: Any = json.loads(mark.read_text())
    except (OSError, ValueError):
        return set()
    return (
        {v for v in cast(dict[str, Any], data).values() if isinstance(v, str)}
        if isinstance(data, dict)
        else set()
    )


def _target(folder: Path, ledger: Path, digest: str, leftover: bool) -> Target:
    side = sidecar(ledger)
    mark = folder / SHIPPED / ledger.stem
    keys: list[str] = []
    if mark.exists():
        with contextlib.suppress(OSError, ValueError):
            data: Any = json.loads(mark.read_text())
            if isinstance(data, dict):
                keys = sorted(str(k) for k in cast(dict[str, Any], data))
    return Target(
        ledger=ledger,
        ledger_sha256=digest,
        sidecar=side if side.exists() else None,
        sidecar_sha256=_sha256(side) if side.exists() else None,
        mark=mark if mark.exists() else None,
        mark_sha256=_sha256(mark) if mark.exists() else None,
        object_keys=keys,
        leftover=leftover,
    )


def _read_state(folder: Path) -> dict[str, Any]:
    path = folder / STATE
    if not path.exists():
        return {}
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


# -- the importer's suppression list ---------------------------------------------------------


def read_erased(folder: Path) -> set[str]:
    """The suppression list. Raises ValueError when it exists and cannot be read: an import
    must stop rather than bring an erased person's conversations back."""
    path = folder / ERASED
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").split()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: {exc}; refusing to import until it is readable") from exc
    if not all(len(h) == 64 and all(c in "0123456789abcdef" for c in h) for h in lines):
        raise ValueError(f"{path}: not a list of SHA-256 hashes; refusing to import")
    return set(lines)


def _add_erased(folder: Path, principals: Iterable[str]) -> None:
    path = folder / ERASED
    hashes = read_erased(folder) | {id_hash(p) for p in principals}
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)  # created afresh, so owner-only whatever an old one allowed
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(f"{h}\n" for h in sorted(hashes)))
    os.replace(tmp, path)


def _write_state(folder: Path, state: dict[str, Any]) -> None:
    path = folder / STATE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# -- erasing --------------------------------------------------------------------------------


def erase(
    folder: Path,
    principals: Iterable[str],
    case: str,
    signer: Signer,
    by: str,
    now: datetime | None = None,
) -> Result:
    """Remove what `plan` finds, inside a signed erasure record. The caller holds the folder's
    lock. A record left open by an interrupted erase is closed first; the files it named are
    found again by their hashes."""
    principals = list(principals)
    closed = close_open_chains(folder, signer, only=is_record, reason="erase interrupted")
    p = plan(folder, principals)
    result = Result(
        folder=folder, record=None, closed=[c.stem for c in closed], to_check=p.to_check
    )
    if p.importer:
        _add_erased(folder, principals)  # before anything goes: an import must never restore it
    if p.empty:
        return result
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{PREFIX}{stamp}-{secrets.token_hex(4)}"
    meta = {
        "principal.id": PRINCIPAL,
        "erasure.case": case,
        "erasure.by": by,
        "erasure.ledgers": len(p.targets),
    }
    with Recorder.start(
        folder, agent_id="seatbelt-erase", run_id=run_id, metadata=meta, signer=signer
    ) as rec:
        # every hash first: a run killed partway leaves a record naming all it meant to remove
        for t in p.targets:
            rec.action(
                "erase ledger",
                f"sha256:{t.ledger_sha256}",
                attrs={
                    "erasure.ledger_sha256": t.ledger_sha256,
                    "erasure.sidecar_sha256": t.sidecar_sha256,
                    "erasure.mark_sha256": t.mark_sha256,
                    "erasure.leftover": t.leftover,
                },
            )
        for t in p.targets:
            t.ledger.unlink(missing_ok=True)
            result.ledgers += 1
            if t.sidecar is not None:
                t.sidecar.unlink(missing_ok=True)
                result.sidecars += 1
            if t.mark is not None:
                t.mark.unlink(missing_ok=True)
                result.marks += 1
        for orphan in p.orphans:
            orphan.unlink(missing_ok=True)
            if orphan.parent.name == SHIPPED:
                result.marks += 1
            else:
                result.sidecars += 1
        if p.state_entries:
            state = _read_state(folder)
            conversations = as_dict(state.get("conversations"))
            for key in p.state_entries:
                conversations.pop(key, None)
            state["conversations"] = conversations
            _write_state(folder, state)
            result.state_entries = len(p.state_entries)
        rec.outcome(
            f"erased {result.ledgers} ledgers, {result.sidecars} signatures, {result.marks} "
            f"shipped marks, {result.state_entries} importer entries",
            success=True,
            attrs={
                "erasure.ledgers": result.ledgers,
                "erasure.sidecars": result.sidecars,
                "erasure.marks": result.marks,
                "erasure.state_entries": result.state_entries,
            },
        )
    result.record = folder / f"{run_id}.jsonl"
    return result
