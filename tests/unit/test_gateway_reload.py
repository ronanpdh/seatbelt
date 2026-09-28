import logging
import os
import signal
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, keygen
from seatbelt.gateway import serve as serve_mod
from seatbelt.gateway.app import Live, create_app
from seatbelt.gateway.config import add_principal, load_config
from seatbelt.gateway.reload import Reloader
from seatbelt.gateway.sessions import Sessions
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.verify.chain import verify_file

BASE = (
    "ledgers: runs\nsession_idle: 900\n"
    "upstreams:\n  anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}\n"
)
REPLY: dict[str, Any] = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def _upstream() -> httpx2.MockTransport:
    return httpx2.MockTransport(lambda _: httpx2.Response(200, json=REPLY))


@dataclass
class Running:
    path: Path
    app: Starlette
    client: TestClient
    sessions: Sessions
    reloader: Reloader
    alice: str
    ledgers: Path

    def ask(self, key: str, model: str = "claude-sonnet-5", run: str | None = None) -> int:
        headers = {"x-api-key": key, **({"x-seatbelt-run": run} if run else {})}
        body = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
        return self.client.post("/v1/messages", json=body, headers=headers).status_code

    def edit(self, old: str, new: str) -> None:
        text = self.path.read_text()
        assert old in text
        self.path.write_text(text.replace(old, new))

    @property
    def live(self) -> Live:
        return self.app.state.live


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Running]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REAL0000000000000000000000")
    path = tmp_path / "gateway.yaml"
    path.write_text(BASE)
    alice = add_principal(path, "alice@corp")
    cfg = load_config(path)
    sessions = Sessions(cfg.ledgers, Signer.generate(), idle=cfg.session_idle)
    app = create_app(cfg, sessions, transport=_upstream())
    with TestClient(app) as client:
        reloader = Reloader(path, app, sessions)
        yield Running(path, app, client, sessions, reloader, alice, cfg.ledgers)
    assert sessions.close_all(timeout=1) == 0


def _drop(path: Path, principal: str) -> None:
    """Delete a principal's entry, as an operator revoking a key would."""
    cfg = load_config(path)
    kept = [p for p in cfg.principals if p.id != principal]
    text = path.read_text()
    head = text[: text.index("principals:")]
    entries = "".join(
        f"- id: {p.id}\n  key_sha256: {p.key_sha256}\n  issued: {p.issued}\n" for p in kept
    )
    path.write_text(head + ("principals:\n" + entries if kept else "principals: []\n"))


def test_an_unchanged_file_is_not_reloaded_unless_forced(gw: Running) -> None:
    before = gw.live
    assert not gw.reloader.check()
    assert gw.live is before
    assert gw.reloader.check(force=True)  # SIGHUP reloads even an unchanged file
    assert gw.live is not before


def test_a_new_key_works_after_reload(gw: Running) -> None:
    bob = add_principal(gw.path, "bob@corp")
    assert gw.ask(bob) == 401  # not yet reloaded
    assert gw.reloader.check()
    assert gw.ask(bob) == 200 and gw.ask(gw.alice) == 200


def test_a_revoked_key_is_refused_and_its_sessions_are_closed_and_signed(gw: Running) -> None:
    assert gw.ask(gw.alice) == 200
    assert gw.ask(gw.alice, run="named") == 200
    _drop(gw.path, "alice@corp")
    assert gw.reloader.check()
    assert gw.ask(gw.alice) == 401
    ledgers = sorted(gw.ledgers.glob("*.jsonl"))
    assert len(ledgers) == 2  # the unnamed session and the named run, both ended
    for ledger in ledgers:
        assert list(read_events(ledger))[-1].kind is Kind.RUN_END
        assert verify_file(ledger).complete and sidecar(ledger).exists()


def test_a_reissued_key_starts_a_new_ledger_under_the_new_key(gw: Running) -> None:
    assert gw.ask(gw.alice) == 200
    _drop(gw.path, "alice@corp")
    new_key = add_principal(gw.path, "alice@corp")
    assert gw.reloader.check()
    assert gw.ask(gw.alice) == 401 and gw.ask(new_key) == 200
    ledgers = [list(read_events(p)) for p in gw.ledgers.glob("*.jsonl")]
    assert len({events[0].attrs["principal.key_id"] for events in ledgers}) == 2  # one per key
    closed = [events for events in ledgers if events[-1].kind is Kind.RUN_END]
    assert len(closed) == 1  # the old key's session ended at reload; the new one is open


def test_policy_and_session_idle_apply_to_the_next_request(gw: Running) -> None:
    assert gw.ask(gw.alice, model="claude-opus-5-5") == 200
    gw.edit("session_idle: 900\n", "session_idle: 60\npolicy:\n  models: [claude-sonnet-5]\n")
    assert gw.reloader.check()
    assert gw.ask(gw.alice, model="claude-opus-5-5") == 403
    assert gw.ask(gw.alice, model="claude-sonnet-5") == 200
    assert gw.sessions.idle == 60


def test_a_broken_file_keeps_the_running_config_and_is_reported_once(
    gw: Running, caplog: pytest.LogCaptureFixture
) -> None:
    good = gw.path.read_text()
    before = gw.live
    gw.path.write_text(good + "unknown_key: 1\n")
    with caplog.at_level(logging.ERROR, logger="seatbelt.gateway.reload"):
        assert not gw.reloader.check()
        assert not gw.reloader.check()  # the same broken file: no second report
    assert len([r for r in caplog.records if "not reloaded" in r.message]) == 1
    assert gw.live is before and gw.ask(gw.alice) == 200
    gw.path.unlink()  # mid-rewrite, say
    assert not gw.reloader.check() and gw.ask(gw.alice) == 200
    gw.path.write_text(good.replace("session_idle: 900", "session_idle: 61"))
    assert gw.reloader.check() and gw.sessions.idle == 61


def test_restart_only_settings_are_reported_and_kept(
    gw: Running, caplog: pytest.LogCaptureFixture
) -> None:
    bob = add_principal(gw.path, "bob@corp")
    gw.edit("ledgers: runs\n", "ledgers: elsewhere\nlisten: 0.0.0.0:9000\n")
    with caplog.at_level(logging.WARNING, logger="seatbelt.gateway.reload"):
        assert gw.reloader.check()
    assert any("listen, ledgers changed; restart" in r.message for r in caplog.records)
    assert gw.live.cfg.ledgers == gw.ledgers and gw.live.cfg.listen == "127.0.0.1:8080"
    assert gw.ask(bob) == 200  # the rest of the file still applied
    assert not (gw.path.parent / "elsewhere").exists()


def _status(client: TestClient, key: str) -> int:
    return client.post("/v1/messages", json={"model": "m"}, headers={"x-api-key": key}).status_code


def _until_accepted(client: TestClient, key: str) -> int:
    for _ in range(200):
        if (status := _status(client, key)) != 401:
            return status
        threading.Event().wait(0.01)
    return 401


def test_serve_reloads_on_change_and_on_sighup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keygen(tmp_path / "keys")
    path = tmp_path / "gateway.yaml"
    path.write_text("signing_key: keys/seatbelt.key\n" + BASE)
    alice = add_principal(path, "alice@corp")
    cfg = load_config(path)
    monkeypatch.setattr(serve_mod, "RELOAD_EVERY", 0.02)
    previous = signal.getsignal(signal.SIGHUP)
    statuses: list[int] = []

    def fake_run(app: Starlette, **_: object) -> None:
        app.state.transport = _upstream()
        with TestClient(app) as client:
            statuses.append(_until_accepted(client, add_principal(path, "bob@corp")))  # polled
            monkeypatch.setattr(serve_mod, "RELOAD_EVERY", 3600.0)  # from here, only SIGHUP
            threading.Event().wait(0.1)  # the watcher is now in its long wait
            carol = add_principal(path, "carol@corp")
            threading.Event().wait(0.1)
            statuses.append(_status(client, carol))  # changed, but nobody looked
            os.kill(os.getpid(), signal.SIGHUP)
            statuses.append(_until_accepted(client, carol))
            statuses.append(_status(client, alice))

    monkeypatch.setattr(serve_mod.uvicorn, "run", fake_run)
    serve_mod.serve(cfg, path)
    assert statuses == [200, 401, 200, 200]
    assert signal.getsignal(signal.SIGHUP) == previous  # restored on the way out
