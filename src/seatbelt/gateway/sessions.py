"""One ledger per employee session. Opened on first request, closed on idle, end, or shutdown."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Generator, Iterable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from seatbelt import __version__
from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, attest
from seatbelt.gateway.formats import Format
from seatbelt.ledger.events import Actor, ActorType, Event, Kind
from seatbelt.ledger.store import Ledger, LedgerError, read_events
from seatbelt.locks import try_lock
from seatbelt.record.recorder import Recorder
from seatbelt.verify.chain import verify_events

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_GATEWAY = "gateway"  # the agent id on every ledger the gateway writes
# in the ledgers folder: a lock per open ledger while a process closes it, removed after
CLOSING = ".closing"
_log = logging.getLogger(__name__)


def _slug(text: str) -> str:
    return _UNSAFE.sub("_", text).strip("_")[:40] or "x"


class DeniedCalls:
    """Tool calls the policy denied, by call id: a principal's, across its sessions. A
    Responses client that chains `previous_response_id` sends a tool's output without the call
    it answers, possibly after the session that saw the call has closed on idle. The newest
    `limit` are kept; a restart forgets them (the history's tool name still applies)."""

    def __init__(self, limit: int = 10_000) -> None:
        self._calls: OrderedDict[str, str] = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def __setitem__(self, call_id: str, tool: str) -> None:
        with self._lock:
            self._calls[call_id] = tool
            self._calls.move_to_end(call_id)
            while len(self._calls) > self._limit:
                self._calls.popitem(last=False)

    def get(self, call_id: str) -> str | None:
        with self._lock:
            return self._calls.get(call_id)


@dataclass(eq=False)  # identity, not field equality: sessions are tracked by object
class Session:
    rec: Recorder
    stack: ExitStack
    lock: threading.Lock = field(default_factory=threading.Lock)
    last: float = 0.0
    formats: dict[str, Format] = field(default_factory=dict[str, Format])  # open tool calls
    denied_calls: DeniedCalls = field(default_factory=DeniedCalls)  # the principal's
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
        on_close: Callable[[Path], None] | None = None,
    ) -> None:
        """`on_close` gets each ledger once it is closed and signed (the sink ships it)."""
        self._root = root
        self._signer = signer
        self._idle = idle
        self._clock = clock
        self._on_close = on_close
        self._open: dict[_Key, Session] = {}
        self._denied: dict[str, DeniedCalls] = {}  # principal -> its denied calls
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
        denied = self._denied.setdefault(principal, DeniedCalls())  # under self._lock
        return Session(rec=rec, stack=stack, last=self._clock(), denied_calls=denied)

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
        self._close_each(ready)
        return len(sessions)

    def sweep(self) -> int:
        now = self._clock()
        with self._lock:
            stale = [k for k, s in self._open.items() if not s.busy and now - s.last > self._idle]
            sessions = [self._open.pop(k) for k in stale]
            for s in sessions:
                s.closing = True
        self._close_each(sessions)
        return len(sessions)

    def close_all(self, timeout: float = 30.0) -> int:
        """Close idle sessions, wait up to `timeout` seconds for busy ones to be released.
        Returns how many were left open; `close_open_chains` finishes them on next start."""
        with self._lock:
            sessions = list(self._open.values())
            self._open.clear()
            ready = [s for s in sessions if self._retire(s)]
        self._close_each(ready)
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

    def _close_each(self, sessions: Iterable[Session]) -> None:
        for s in sessions:
            try:
                self._close(s)
            except Exception:  # one ledger that fails to close must not leave the rest open
                _log.exception("closing %s failed", s.rec.ledger.path)

    def _close(self, session: Session) -> None:
        try:
            with session.lock:  # never close mid-request
                session.stack.close()
            if self._on_close is not None:
                try:
                    self._on_close(session.rec.ledger.path)
                except Exception:  # shipping is best effort; the next start catches up
                    _log.exception("on_close failed for %s", session.rec.ledger.path)
        finally:
            with self._changed:
                if session in self._draining:
                    self._draining.remove(session)
                self._changed.notify_all()


def _events(path: Path) -> list[Event] | None:
    """A ledger's events, or None, logged, when it cannot be read."""
    try:
        return list(read_events(path))
    except (OSError, UnicodeDecodeError, LedgerError) as exc:
        _log.warning("skipping unreadable ledger %s: %s", path, exc)
        return None


@contextlib.contextmanager
def _closing(root: Path, path: Path) -> Generator[bool]:
    """Hold `<root>/.closing/<ledger>.lock` while the block closes `path`, so two processes
    tidying one folder never both append a run.end. Yields False when another process holds
    it. The file is removed while still held: one who opened it before then finds, once it
    has the lock, that it no longer names that file, and yields False as well."""
    lock = root / CLOSING / f"{path.name}.lock"
    lock.parent.mkdir(exist_ok=True)
    with lock.open("ab") as handle:
        try:
            mine = try_lock(handle) and os.path.samestat(os.fstat(handle.fileno()), lock.stat())
        except FileNotFoundError:  # removed by its holder since it was opened
            mine = False
        if not mine:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):  # Windows cannot remove an open file
                lock.unlink()


def close_open_chains(
    root: Path,
    signer: Signer | None,
    only: Callable[[list[Event]], bool] | None = None,
    reason: str = "gateway restarted",
) -> list[Path]:
    """After a crash: append a failed run.end to every open chain and sign it. `only`, given
    the chain's events, picks which open chains to close: those of runs no longer alive,
    where several processes record into the same folder (`seatbelt run`). A ledger that
    cannot be read or whose chain is broken is left untouched and logged: closing it would
    put a signature over evidence of tampering, and refusing to start would let one bad file
    stop all recording. A closed ledger is never signed here, even one of the gateway's with
    no signature: a process killed between run.end and signing looks the same as a ledger
    rewritten and its signature deleted, so it is logged for a person to check. A ledger
    another process is closing at the same time is left to it."""
    closed: list[Path] = []
    for path in sorted(root.glob("*.jsonl")):
        events = _events(path)
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
        if only is not None and not only(events):
            continue
        with _closing(root, path) as mine:
            # read again under the lock: another process may have closed it meanwhile
            events = _events(path) if mine else None
            if not events or events[-1].kind is Kind.RUN_END:
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
                {"run.ok": False, "run.error": reason, "run.events": len(events) + 1},
            )
            if signer is not None and not sidecar(path).exists():
                attest(path, signer)
            closed.append(path)
    return closed
