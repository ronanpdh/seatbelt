"""Run the gateway under uvicorn. Kept out of cli.py so the CLI imports no server code."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import threading
from collections.abc import Mapping

import uvicorn

from seatbelt.attest.manifest import AttestError
from seatbelt.attest.sign import Signer
from seatbelt.gateway.app import create_app
from seatbelt.gateway.config import GatewayConfig
from seatbelt.gateway.sessions import Sessions, close_open_chains

SWEEP_EVERY = 30.0  # seconds; a session closes at most this long after its idle window
DRAIN = 30  # seconds uvicorn, then close_all, wait for in-flight requests on shutdown

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
    if value.startswith("-----BEGIN"):
        # some env var UIs flatten a multi-line value into backslash-n sequences
        return Signer.from_pem(value.replace("\\n", "\n").encode(), KEY_ENV)
    try:
        pem = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttestError(f"{KEY_ENV}: neither a PEM key nor base64 of one") from exc
    return Signer.from_pem(pem, KEY_ENV)


def serve(cfg: GatewayConfig) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
    signer = load_signer(cfg, os.environ)
    host, port = _host_port(cfg.listen)
    closed = close_open_chains(cfg.ledgers, signer)
    if closed:
        _log.warning("closed %d chains left open by a previous run", len(closed))
    sessions = Sessions(cfg.ledgers, signer, idle=cfg.session_idle)
    stop = threading.Event()

    def sweeper() -> None:
        while not stop.wait(SWEEP_EVERY):
            try:
                sessions.sweep()
            except Exception:  # a failed close must not stop every later close
                _log.exception("session sweep failed")

    threading.Thread(target=sweeper, name="seatbelt-sweeper", daemon=True).start()
    try:
        uvicorn.run(
            create_app(cfg, sessions),
            host=host,
            port=port,
            log_level="info",
            timeout_graceful_shutdown=DRAIN,
        )
    finally:
        stop.set()
        left = sessions.close_all(timeout=DRAIN)
        if left:
            _log.warning("%d sessions still busy at shutdown; closed on next start", left)
