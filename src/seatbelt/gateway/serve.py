"""Run the gateway under uvicorn. Kept out of cli.py so the CLI imports no server code."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import signal
import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from seatbelt.attest.manifest import AttestError
from seatbelt.attest.sign import Signer
from seatbelt.gateway.app import create_app
from seatbelt.gateway.config import GatewayConfig, read_config
from seatbelt.gateway.reload import Reloader
from seatbelt.gateway.sessions import Sessions, close_open_chains
from seatbelt.gateway.sink import S3Store, Sink
from seatbelt.locks import hold_folder

SWEEP_EVERY = 30.0  # seconds; a session closes at most this long after its idle window
DRAIN = 30  # seconds uvicorn, then close_all, wait for in-flight requests on shutdown
RELOAD_EVERY = 30.0  # seconds between checks of the config file; SIGHUP checks at once

_log = logging.getLogger(__name__)


def _host_port(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    return host.strip("[]") or "127.0.0.1", int(port)  # "[::]:8080" is IPv6


KEY_ENV = "SEATBELT_SIGNING_KEY"


def load_signer(cfg: GatewayConfig, env: Mapping[str, str]) -> Signer:
    """The signing key from `signing_key` in the config, or from SEATBELT_SIGNING_KEY: the PEM
    itself or its base64 (which survives any env var UI). For hosts that inject secrets as
    environment, where a mounted key file's owner and mode are not under your control."""
    value = env.get(KEY_ENV, "").strip()
    if value and cfg.signing_key is not None:
        raise AttestError(f"both signing_key and {KEY_ENV} are set; set one")
    if cfg.signing_key is not None:
        return Signer.from_file(cfg.signing_key)
    if not value:
        raise AttestError(f"no signing key: set signing_key in the config or {KEY_ENV}")
    return _signer_from_env(value)


_B64 = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


def _signer_from_env(raw: str) -> Signer:
    """Forgives what editors and terminals add: surrounding quotes, and any character that is
    not base64. Errors describe the value's shape, never its content."""
    value = raw
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    if value.startswith("-----BEGIN"):
        # some env var UIs flatten a multi-line value into backslash-n sequences
        return Signer.from_pem(value.replace("\\n", "\n").encode(), KEY_ENV)
    # a character outside base64 is never part of the key: whitespace from a wrapped paste, or
    # the "%" zsh prints after output with no final newline. Dropping it cannot turn a wrong
    # value into a valid key; the PEM parse below still has to succeed.
    stray = sorted({c for c in value if c not in _B64})
    if stray:
        _log.warning(
            "%s: ignoring characters that are not base64: %s", KEY_ENV, ", ".join(map(repr, stray))
        )
    compact = "".join(c for c in value if c in _B64)
    try:
        pem = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttestError(f"{KEY_ENV}: neither a PEM key nor base64 of one; {_shape(raw)}") from exc
    return Signer.from_pem(pem, f"{KEY_ENV} (decoded from base64; {_shape(raw)})")


def _shape(raw: str) -> str:
    stray = sorted({c for c in raw if c not in _B64})
    compact = "".join(c for c in raw if c in _B64)
    return (
        f"{len(raw)} characters, "
        f"{'starts' if compact.startswith('LS0tLS1CRUdJT') else 'does not start'} like base64 "
        f"of a PEM key, characters that are not base64: {', '.join(map(repr, stray)) or 'none'}"
        + ("" if len(compact) % 4 == 0 else ", length is not a multiple of 4 (cut short?)")
    )


@contextmanager
def _on_sighup(action: Callable[[], None]) -> Generator[bool]:
    """Run `action` on SIGHUP until the block ends, then put the previous handler back.
    Without a handler SIGHUP kills the process, or, as PID 1 in a container, is ignored.
    Yields whether it was installed: Windows has no SIGHUP, and only the main thread may set
    handlers."""
    if not hasattr(signal, "SIGHUP") or threading.current_thread() is not threading.main_thread():
        yield False
        return
    previous = signal.signal(signal.SIGHUP, lambda _signum, _frame: action())
    try:
        yield True
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_DFL if previous is None else previous)


@contextmanager
def _graceful_stop() -> Generator[None]:
    """uvicorn stops on SIGINT, SIGTERM or (Windows) SIGBREAK, puts back the handlers it
    found, and raises the signal again. Under a default handler that ends the process before
    its sessions are closed and signed, so uvicorn finds handlers that do nothing, and they
    stay until every session is signed: a second signal then would leave a closed ledger
    unsigned, which no restart may sign (see `close_open_chains`). SIGKILL still stops it."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    stops = [getattr(signal, n) for n in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, n)]
    previous = {sig: signal.signal(sig, lambda _signum, _frame: None) for sig in stops}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)


def make_sink(cfg: GatewayConfig, env: Mapping[str, str], root: Path | None = None) -> Sink | None:
    """The configured sink, shipping ledgers from `root` (default: the gateway's)."""
    if cfg.sink is None:
        return None
    s = cfg.sink
    missing = [n for n in (s.access_key_env, s.secret_key_env) if not env.get(n)]
    if missing:
        raise ValueError(f"sink: set {', '.join(missing)} in the environment")
    store = S3Store(s.url, s.bucket, s.region, env[s.access_key_env], env[s.secret_key_env])
    return Sink(store, cfg.ledgers if root is None else root, prefix=s.prefix)


def serve(path: Path) -> None:
    """Run the gateway on the config at `path`, and reload the file when it changes."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
    stop = threading.Event()
    hup = threading.Event()
    # from the start: a SIGHUP during the startup scan leaves `hup` set, and the watcher's
    # first wait then reloads
    with _on_sighup(hup.set) as sighup:
        cfg, loaded = read_config(path)  # the watcher compares the file against these bytes
        # `seatbelt erase` needs the folder to itself; a second gateway on it is unsupported
        lock = hold_folder(cfg.ledgers, "the gateway")
        signer = load_signer(cfg, os.environ)
        host, port = _host_port(cfg.listen)
        closed = close_open_chains(cfg.ledgers, signer)
        if closed:
            _log.warning("closed %d chains left open by a previous run", len(closed))
        sink = make_sink(cfg, os.environ)
        if sink is not None:
            pending = sink.catch_up()  # includes the chains just closed
            if pending:
                _log.info("shipping %d ledgers not shipped yet", pending)
            sink.start()
        sessions = Sessions(
            cfg.ledgers,
            signer,
            idle=cfg.session_idle,
            on_close=sink.ship if sink is not None else None,
        )
        app = create_app(cfg, sessions)
        reloader = Reloader(path, app, sessions, loaded)

        def sweeper() -> None:
            while not stop.wait(SWEEP_EVERY):
                try:
                    sessions.sweep()
                except Exception:  # a failed close must not stop every later close
                    _log.exception("session sweep failed")

        def watcher() -> None:
            while True:
                signalled = hup.wait(RELOAD_EVERY)
                hup.clear()
                if stop.is_set():
                    return
                try:
                    reloader.check(force=signalled)
                except Exception:  # a failed reload must not stop every later one
                    _log.exception("config reload failed")

        threads = [
            threading.Thread(target=sweeper, name="seatbelt-sweeper", daemon=True),
            threading.Thread(target=watcher, name="seatbelt-reload", daemon=True),
        ]
        for thread in threads:
            thread.start()
        with _graceful_stop():
            try:
                uvicorn.run(
                    app,
                    host=host,
                    port=port,
                    log_level="info",
                    timeout_graceful_shutdown=DRAIN,
                )
            finally:
                if sighup:  # a late SIGHUP must not run its handler inside hup.set below
                    signal.signal(signal.SIGHUP, signal.SIG_IGN)
                stop.set()
                hup.set()  # wake the watcher so it sees stop
                for thread in threads:  # a sweep or reload may be signing a ledger it closed
                    thread.join(timeout=DRAIN)
                left = sessions.close_all(timeout=DRAIN)
                if left:
                    _log.warning("%d sessions still busy at shutdown; closed on next start", left)
                if sink is not None:
                    unsent = sink.stop(timeout=DRAIN)
                    if unsent:
                        _log.warning("%d ledgers not shipped yet; shipped on next start", unsent)
                lock.close()  # only now: every session is closed and signed
