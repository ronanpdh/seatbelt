"""Apply a changed config file to a running gateway, without a restart.

Keys, policy, upstreams and `session_idle` take effect for the next request. `listen`,
`ledgers` and `signing_key` are bound at start: a change to them is logged and waits for a
restart. A file that fails to load is logged and the running config stays."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from starlette.applications import Starlette

from seatbelt.gateway.app import Live
from seatbelt.gateway.config import GatewayConfig, load_config
from seatbelt.gateway.sessions import Sessions

RESTART_ONLY = ("listen", "ledgers", "signing_key")

_log = logging.getLogger(__name__)


class Reloader:
    """Not thread-safe: one thread calls `check`."""

    def __init__(self, path: Path, app: Starlette, sessions: Sessions) -> None:
        self._path = path
        self._app = app
        self._sessions = sessions
        self._seen = self._digest()  # the file the gateway started with

    def _digest(self) -> str | None:
        try:
            return hashlib.sha256(self._path.read_bytes()).hexdigest()
        except OSError:
            return None

    def check(self, force: bool = False) -> bool:
        """Reload if the file's content changed since it was last seen, or when `force`d (a
        SIGHUP). A file that fails is reported once, not on every check. True if applied."""
        try:
            data = self._path.read_bytes()
        except OSError as exc:
            if self._seen is not None or force:
                _log.error("config not reloaded, keeping the running one: %s", exc)
            self._seen = None
            return False
        digest = hashlib.sha256(data).hexdigest()
        if digest == self._seen and not force:
            return False
        self._seen = digest
        try:
            new = load_config(self._path, data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            _log.error("config not reloaded, keeping the running one: %s", exc)
            return False
        self.apply(new)
        return True

    def apply(self, new: GatewayConfig) -> None:
        old = self.current
        fixed = [name for name in RESTART_ONLY if getattr(old, name) != getattr(new, name)]
        if fixed:
            _log.warning("config: %s changed; restart the gateway to apply", ", ".join(fixed))
        kept = new.model_copy(update={name: getattr(old, name) for name in RESTART_ONLY})
        # swap first, so a withdrawn key is refused before its sessions are ended. A request
        # it authenticated just before the swap still finishes, and any session that request
        # opens closes on idle
        self._app.state.live = Live.of(kept)
        self._sessions.idle = new.session_idle
        before = {(p.id, p.key_sha256) for p in old.principals}
        after = {(p.id, p.key_sha256) for p in new.principals}
        withdrawn = sorted({pid for pid, _ in before - after})
        ended = sum(self._sessions.end_principal(pid) for pid in withdrawn)
        _log.info(
            "config reloaded: %d principals; new keys: %s; keys withdrawn: %s (%d sessions ended)",
            len(new.principals),
            ", ".join(sorted({pid for pid, _ in after - before})) or "none",
            ", ".join(withdrawn) or "none",
            ended,
        )

    @property
    def current(self) -> GatewayConfig:
        live: Live = self._app.state.live
        return live.cfg
