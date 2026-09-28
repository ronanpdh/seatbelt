"""One ledger per employee session. Opened on first request, closed on idle, end, or shutdown."""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from seatbelt import __version__
from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, attest
from seatbelt.gateway.formats import Format
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, LedgerError, read_events
from seatbelt.record.recorder import Recorder
from seatbelt.verify.chain import verify_events

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_GATEWAY = "gateway"  # the agent id on every ledger the gateway writes
_log = logging.getLogger(__name__)


def _slug(text: str) -> str:
    return _UNSAFE.sub("_", text).strip("_")[:40] or "x"


@dataclass(eq=False)  # identity, not field equality: sessions are tracked by object
class Session:
    rec: Recorder
    stack: ExitStack
    lock: threading.Lock = field(default_factory=threading.Lock)
    last: float = 0.0
    formats: dict[str, Format] = field(default_factory=dict[str, Format])  # open tool calls
    denied_calls: dict[str, str] = field(default_factory=dict[str, str])  # call id -> tool
    busy: int = 0  # requests between Sessions.get and Sessions.release
    closing: bool = False


type _Key = tuple[str, str, str | None]  # (principal, issued key's hash, run name)


class Sessions:
    """Every `get` must be paired with one `release` once the response is recorded. A session
    is never closed while a request holds it, so no event can land after its `run.end`.

    A session belongs to one issued key as well as one principal: a reissued key never writes
    into a ledger the old key opened, so each ledger's `principal.key_id` is the key that
    wrote all of it."""

    def __init__(
        self,
        root: Path,
        signer: Signer | None,
        idle: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = root
        self._signer = signer
        self._idle = idle
        self._clock = clock
        self._open: dict[_Key, Session] = {}
        self._draining: list[Session] = []  # ended while busy; the last release closes them
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        root.mkdir(parents=True, exist_ok=True)

    @property
    def idle(self) -> float:
        return self._idle

    @idle.setter
    def idle(self, seconds: float) -> None:  # a config reload; the next sweep uses it
        self._idle = seconds

    def get(self, principal: str, run: str | None, meta: dict[str, Any], key: str = "") -> Session:
        with self._lock:
            session = self._open.get((principal, key, run))
            if session is None:
                session = self._start(principal, run, meta)
                self._open[(principal, key, run)] = session
            session.busy += 1
            session.last = self._clock()
            return session

    def release(self, session: Session) -> None:
        with self._lock:
            session.busy -= 1
            session.last = self._clock()
            ready = session.closing and session.busy == 0
        if ready:
            self._close(session)

    def _start(self, principal: str, run: str | None, meta: dict[str, Any]) -> Session:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        run_id = f"{_slug(principal)}-{_slug(run) if run else stamp}-{secrets.token_hex(4)}"
        stack = ExitStack()
        rec = stack.enter_context(
            Recorder.start(
                self._root,
                agent_id=_GATEWAY,
                agent_version=__version__,
                run_id=run_id,
                # identity last: client-derived metadata must not overwrite who this is
                metadata={**meta, "principal.id": principal, "run.name": run},
                signer=self._signer,
            )
        )
        return Session(rec=rec, stack=stack, last=self._clock())

    def end(self, principal: str, run: str | None, key: str = "") -> bool:
        """Close now, or at the last release if a request is in flight. The next `get` for the
        same session starts a new ledger either way."""
        with self._lock:
            session = self._open.pop((principal, key, run), None)
            ready = session is not None and self._retire(session)
        if session is None:
            return False
        if ready:
            self._close(session)
        return True

    def end_principal(self, principal: str) -> int:
        """End every session of `principal`, under any key, each now or at its last release:
        its key was revoked or reissued. Returns how many were ended."""
        with self._lock:
            sessions = [self._open.pop(k) for k in [k for k in self._open if k[0] == principal]]
            ready = [s for s in sessions if self._retire(s)]
        for s in ready:
            self._close(s)
        return len(sessions)

    def sweep(self) -> int:
        now = self._clock()
        with self._lock:
            stale = [k for k, s in self._open.items() if not s.busy and now - s.last > self._idle]
            sessions = [self._open.pop(k) for k in stale]
            for s in sessions:
                s.closing = True
        for s in sessions:
            self._close(s)
        return len(sessions)

    def close_all(self, timeout: float = 30.0) -> int:
        """Close idle sessions, wait up to `timeout` seconds for busy ones to be released.
        Returns how many were left open; `close_open_chains` finishes them on next start."""
        with self._lock:
            sessions = list(self._open.values())
            self._open.clear()
            ready = [s for s in sessions if self._retire(s)]
        for s in ready:
            self._close(s)
        with self._changed:
            self._changed.wait_for(lambda: not self._draining, timeout)
            return len(self._draining)

    def _retire(self, session: Session) -> bool:
        """Mark for closing; True if idle and the caller should close it. Hold `_lock`."""
        session.closing = True
        if session.busy:
            self._draining.append(session)
            return False
        return True

    def _close(self, session: Session) -> None:
        try:
            with session.lock:  # never close mid-request
                session.stack.close()
        finally:
            with self._changed:
                if session in self._draining:
                    self._draining.remove(session)
                self._changed.notify_all()


def close_open_chains(root: Path, signer: Signer | None) -> list[Path]:
    """After a crash: append a failed run.end to every open chain and sign it. A ledger that
    cannot be read or whose chain is broken is left untouched and logged: closing it would
    put a signature over evidence of tampering, and refusing to start would let one bad file
    stop all recording. A closed ledger is never signed here, even one of the gateway's with
    no signature: a process killed between run.end and signing looks the same as a ledger
    rewritten and its signature deleted, so it is logged for a person to check."""
    closed: list[Path] = []
    for path in sorted(root.glob("*.jsonl")):
        try:
            events = list(read_events(path))
        except (OSError, UnicodeDecodeError, LedgerError) as exc:
            _log.warning("skipping unreadable ledger %s: %s", path, exc)
            continue
        if not events:
            continue
        if events[-1].kind is Kind.RUN_END:
            gateway = signer is not None and "principal.id" in events[0].attrs
            if gateway and not sidecar(path).exists():
                _log.warning(
                    "%s is closed but not signed: the gateway was killed while signing it, "
                    "or its signature was deleted. Check it, then sign it with "
                    "`seatbelt attest` if it is genuine",
                    path,
                )
            continue
        verdict = verify_events(events)
        if not verdict.ok:
            _log.warning(
                "skipping broken ledger %s at seq %s: %s",
                path,
                verdict.first_bad_seq,
                verdict.reason,
            )
            continue
        Ledger(path, events[0].run_id).append(
            Kind.RUN_END,
            Actor(type=ActorType.AGENT, id=_GATEWAY, version=__version__),
            {"run.ok": False, "run.error": "gateway restarted", "run.events": len(events) + 1},
        )
        if signer is not None and not sidecar(path).exists():
            attest(path, signer)
        closed.append(path)
    return closed
