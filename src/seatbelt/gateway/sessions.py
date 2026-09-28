"""One ledger per employee session. Opened on first request, closed on idle, end, or shutdown."""

from __future__ import annotations

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
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
from seatbelt.record.recorder import Recorder

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(text: str) -> str:
    return _UNSAFE.sub("_", text).strip("_")[:40] or "x"


@dataclass
class Session:
    rec: Recorder
    stack: ExitStack
    lock: threading.Lock = field(default_factory=threading.Lock)
    last: float = 0.0
    formats: dict[str, Any] = field(default_factory=dict[str, Any])  # per-format state, see app.py
    denied_calls: set[str] = field(default_factory=set[str])  # tool call ids policy refused


class Sessions:
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
        self._open: dict[tuple[str, str | None], Session] = {}
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)

    def get(self, principal: str, run: str | None, meta: dict[str, Any]) -> Session:
        with self._lock:
            session = self._open.get((principal, run))
            if session is None:
                session = self._start(principal, run, meta)
                self._open[(principal, run)] = session
            session.last = self._clock()
            return session

    def _start(self, principal: str, run: str | None, meta: dict[str, Any]) -> Session:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        run_id = f"{_slug(principal)}-{_slug(run) if run else stamp}-{secrets.token_hex(4)}"
        stack = ExitStack()
        rec = stack.enter_context(
            Recorder.start(
                self._root,
                agent_id="gateway",
                agent_version=__version__,
                run_id=run_id,
                # identity last: client-derived metadata must not overwrite who this is
                metadata={**meta, "principal.id": principal, "run.name": run},
                signer=self._signer,
            )
        )
        return Session(rec=rec, stack=stack, last=self._clock())

    def end(self, principal: str, run: str | None) -> bool:
        with self._lock:
            session = self._open.pop((principal, run), None)
        if session is None:
            return False
        self._close(session)
        return True

    def sweep(self) -> int:
        now = self._clock()
        with self._lock:
            stale = [k for k, s in self._open.items() if now - s.last > self._idle]
            sessions = [self._open.pop(k) for k in stale]
        for s in sessions:
            self._close(s)
        return len(sessions)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._open.values())
            self._open.clear()
        for s in sessions:
            self._close(s)

    @staticmethod
    def _close(session: Session) -> None:
        with session.lock:  # never close mid-request
            session.stack.close()


def close_open_chains(root: Path, signer: Signer | None) -> list[Path]:
    """After a crash: append a failed run.end to every open chain and sign it."""
    closed: list[Path] = []
    for path in sorted(root.glob("*.jsonl")):
        events = list(read_events(path))
        if not events or events[-1].kind is Kind.RUN_END:
            continue
        ledger = Ledger(path, events[0].run_id)
        ledger.append(
            Kind.RUN_END,
            Actor(type=ActorType.AGENT, id="gateway", version=__version__),
            {"run.ok": False, "run.error": "gateway restarted", "run.events": len(events) + 1},
        )
        if signer is not None and not sidecar(path).exists():
            attest(path, signer)
        closed.append(path)
    return closed
