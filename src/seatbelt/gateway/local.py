"""The gateway inside `seatbelt run`: record a CLI's traffic on this machine, no server needed.

Started on a free localhost port for one run and stopped when the CLI exits. Every upstream
passes the CLI's own credentials through (an API key or a subscription login), so nothing
is configured but where the ledgers go. The CLI names the run's key in `x-seatbelt-key`: the
port is on localhost, but only this run can write to its ledger."""

from __future__ import annotations

import getpass
import logging
import os
import secrets
import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
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
from seatbelt.gateway.sessions import Sessions

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


def local_signer(keys: Path) -> Signer:
    """This machine's signing key, made on first use (mode 0600) beside its public key."""
    if not (keys / KEY_FILE).exists():
        keygen(keys)
        _log.info("made a signing key for local runs in %s", keys)
    return Signer.from_file(keys / KEY_FILE)


@dataclass
class LocalRecorder:
    """A running local gateway: point the CLI at `url` and send `key` as `x-seatbelt-key`."""

    url: str
    key: str
    principal: str
    ledgers: Path
    sessions: Sessions
    written: list[Path] = field(default_factory=list[Path])  # ledgers closed so far

    def end(self, run: str) -> None:
        self.sessions.end(self.principal, run, key_hash(self.key))


@contextmanager
def local_recorder(
    ledgers: Path,
    keys: Path,
    sink: Mapping[str, object] | None = None,
    upstreams: Mapping[str, str] | None = None,
    env: Mapping[str, str] = os.environ,
) -> Generator[LocalRecorder]:
    """Serve on 127.0.0.1 in a background thread until the block exits, then close and sign
    every ledger this run opened, and ship them when a sink is configured. `upstreams`
    replaces the providers' URLs by name (a corporate proxy, say). Raises ValueError for a
    bad sink or upstream."""
    signer = local_signer(keys)
    urls = {**UPSTREAMS, **(upstreams or {})}
    try:
        sink_cfg = SinkConfig.model_validate(sink) if sink is not None else None
    except ValidationError as exc:
        raise ValueError(f"sink: {exc}") from exc
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    principal = getpass.getuser()
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
        shipper.catch_up()  # earlier runs a sink missed
        shipper.start()
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
    thread.start()
    try:
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("the local recorder did not start")
            thread.join(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield LocalRecorder(f"http://127.0.0.1:{port}", key, principal, ledgers, sessions, written)
    finally:
        server.should_exit = True
        thread.join(30)
        left = sessions.close_all()
        if left:
            _log.warning("%d ledgers were still busy and are left open", left)
        if shipper is not None:
            unshipped = shipper.stop(SHIP_WAIT)
            if unshipped:
                _log.warning("%d ledgers not shipped yet; the next run ships them", unshipped)
