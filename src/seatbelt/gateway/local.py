"""The gateway inside `seatbelt run`: record a CLI's traffic on this machine, no server needed.

Started on a free localhost port for one run and stopped when the CLI exits. Every upstream
passes the CLI's own credentials through (an API key or a subscription login), so nothing
is configured but where the ledgers go. The CLI names the run's key in its base URL's path
(`/_seatbelt/<key>/<run>`), or Codex in `x-seatbelt-key`: the port is on localhost, but only
this run can write to its ledger."""

from __future__ import annotations

import getpass
import logging
import os
import secrets
import threading
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from seatbelt.attest.sign import KEY_FILE, Signer, keygen
from seatbelt.gateway.app import create_app
from seatbelt.gateway.config import (
    KEY_PREFIX,
    GatewayConfig,
    Principal,
    SinkConfig,
    Upstream,
    key_hash,
)
from seatbelt.gateway.serve import make_sink
from seatbelt.gateway.sessions import Sessions, close_open_chains
from seatbelt.locks import RUNNING, try_lock

_log = logging.getLogger(__name__)

UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "chatgpt": "https://chatgpt.com",  # Codex signed in with ChatGPT
    "gemini": "https://generativelanguage.googleapis.com",
    "codeassist": "https://cloudcode-pa.googleapis.com",  # Gemini CLI signed in with Google
}
IDLE = 7 * 24 * 3600.0  # nothing sweeps local sessions; the run's end closes its ledger
SHIP_WAIT = 30.0  # seconds to wait at exit for the sink; what is left ships next run
TIDY_WAIT = 60.0  # seconds to wait at exit for the start-up tidy (below) to finish
DEAD_RUN = "run ended without closing its ledger (the process was killed)"


def _lock_file(ledgers: Path, run: str) -> Path:
    return ledgers / RUNNING / f"{run}.lock"


def _alive(ledgers: Path, run: object) -> bool:
    """Whether the run that opened a ledger is still running: its lock file is locked. A run
    takes its lock before it opens any ledger, so a ledger without one is a dead run's."""
    if not isinstance(run, str) or not run:
        return False
    path = _lock_file(ledgers, run)
    try:
        handle = path.open("rb")
    except OSError:
        return False
    with handle:
        if not try_lock(handle):
            return True
    path.unlink(missing_ok=True)  # stale: its run died
    return False


def close_dead_runs(ledgers: Path, signer: Signer) -> list[Path]:
    """Close and sign the open ledgers of runs that died (killed, or crashed) and leave every
    live run's alone: several runs can record into the same folder at once."""
    return close_open_chains(
        ledgers,
        signer,
        only=lambda events: not _alive(ledgers, events[0].attrs.get("run.name")),
        reason=DEAD_RUN,
    )


def local_signer(keys: Path) -> Signer:
    """This machine's signing key, made on first use (mode 0600) beside its public key."""
    if not (keys / KEY_FILE).exists():
        keygen(keys)
        _log.info("made a signing key for local runs in %s", keys)
    return Signer.from_file(keys / KEY_FILE)


def login_name() -> str:
    """Who is running: the login name, or in a container with none, the numeric user id."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # no USER/LOGNAME and no passwd entry
        return f"uid-{os.getuid()}" if hasattr(os, "getuid") else "unknown"


@dataclass
class LocalRecorder:
    """A running local gateway: point the CLI at `url` with `key` (see `launcher.local_url`)."""

    url: str
    key: str
    principal: str
    ledgers: Path
    sessions: Sessions
    written: list[Path] = field(default_factory=list[Path])  # ledgers closed so far

    def end(self, run: str) -> None:
        self.sessions.end(self.principal, run, key_hash(self.key))


@contextmanager
def _run_lock(ledgers: Path, run: str) -> Generator[None]:
    path = _lock_file(ledgers, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        if not try_lock(handle):
            raise ValueError(f"run {run} is already recording")
        try:
            yield
        finally:
            path.unlink(missing_ok=True)


@contextmanager
def local_recorder(
    ledgers: Path,
    keys: Path,
    run: str,
    sink: Mapping[str, object] | None = None,
    upstreams: Mapping[str, str] | None = None,
    env: Mapping[str, str] = os.environ,
) -> Generator[LocalRecorder]:
    """Serve on 127.0.0.1 in a background thread until the block exits, then close and sign
    every ledger this run opened, and ship them when a sink is configured. `run` is the run's
    name, which its lock is known by. `upstreams` replaces the providers' URLs by name (a
    corporate proxy, say). Raises ValueError for a bad sink or upstream.

    Meanwhile, in the background: ledgers dead runs left open are closed and signed, and a
    sink ships what earlier runs did not."""
    signer = local_signer(keys)
    urls = {**UPSTREAMS, **(upstreams or {})}
    try:
        sink_cfg = SinkConfig.model_validate(sink) if sink is not None else None
    except ValidationError as exc:
        raise ValueError(f"sink: {exc}") from exc
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    principal = login_name()
    cfg = GatewayConfig(
        listen="127.0.0.1:0",
        ledgers=ledgers,
        session_idle=int(IDLE),
        upstreams={name: Upstream(url=url) for name, url in urls.items()},
        principals=[Principal(id=principal, key_sha256=key_hash(key), issued=date.today())],
        sink=sink_cfg,
    )
    shipper = make_sink(cfg, env)
    if shipper is not None:
        shipper.start()

    def tidy() -> None:  # reads every open ledger: off the path of the CLI's start
        closed = close_dead_runs(ledgers, signer)
        if closed:
            _log.warning("closed %d ledgers that runs left open when they were killed", len(closed))
        if shipper is not None:
            shipper.catch_up()  # earlier runs' ledgers a sink missed, and those just closed

    written: list[Path] = []

    def closed(path: Path) -> None:
        written.append(path)
        if shipper is not None:
            shipper.ship(path)

    sessions = Sessions(ledgers, signer, idle=IDLE, on_close=closed)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(cfg, sessions), host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
    )
    thread = threading.Thread(target=server.run, name="seatbelt-local", daemon=True)
    tidier = threading.Thread(target=tidy, name="seatbelt-tidy", daemon=True)
    stack = ExitStack()
    stack.enter_context(_run_lock(ledgers, run))  # before any ledger of this run exists
    thread.start()
    tidier.start()
    try:
        while not server.started:
            if not thread.is_alive():
                raise ValueError("the local recorder did not start; see the log above")
            thread.join(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield LocalRecorder(f"http://127.0.0.1:{port}", key, principal, ledgers, sessions, written)
    finally:
        server.should_exit = True
        thread.join(30)
        left = sessions.close_all()
        if left:
            _log.warning("%d ledgers were still busy; the next run closes them", left)
        tidier.join(TIDY_WAIT)  # a daemon killed mid-append would break a chain
        stack.close()  # this run's ledgers are closed, or now a dead run's for the next one
        if shipper is not None:
            unshipped = shipper.stop(SHIP_WAIT)
            if unshipped:
                _log.warning("%d ledgers not shipped yet; the next run ships them", unshipped)
