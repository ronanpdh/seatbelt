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
from seatbelt.gateway.sessions import Session, Sessions
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
        reloader = Reloader(path, app, sessions, path.read_bytes())
        yield Running(path, app, client, sessions, reloader, alice, cfg.ledgers)
    assert sessions.close_all(timeout=1) == 0


def _drop(path: Path, principal: str) -> None:
    """Delete a principal's three lines, as an operator revoking a key would. Deleting the
    last one leaves `principals:` with no value."""
    lines = path.read_text().splitlines(keepends=True)
    at = lines.index(f"- id: {principal}\n")
    path.write_text("".join(lines[:at] + lines[at + 3 :]))


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


def test_a_revoked_key_is_refused_on_every_endpoint(gw: Running) -> None:
    assert gw.ask(gw.alice, run="named") == 200
    _drop(gw.path, "alice@corp")
    assert gw.path.read_text().rstrip().endswith("principals:")  # null in YAML
    assert gw.reloader.check()
    headers = {"x-api-key": gw.alice}
    assert gw.client.get("/v1/models", headers=headers).status_code == 401
    assert gw.client.post("/seatbelt/runs/named/end", headers=headers).status_code == 401
    assert add_principal(gw.path, "bob@corp")  # keygen still works on the emptied list


def test_a_request_authenticated_before_a_reissue_cannot_share_a_ledger_with_the_new_key(
    gw: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reload lands between an old-key request's authentication and its session."""
    get = gw.sessions.get
    new_key: list[str] = []

    def reissue_first(
        principal: str, run: str | None, meta: dict[str, Any], key: str = ""
    ) -> Session:
        if not new_key:
            _drop(gw.path, "alice@corp")
            new_key.append(add_principal(gw.path, "alice@corp"))
            assert gw.reloader.check()
        return get(principal, run, meta, key)

    monkeypatch.setattr(gw.sessions, "get", reissue_first)
    assert gw.ask(gw.alice) == 200  # authenticated before the reload, so it finishes
    assert gw.ask(new_key[0]) == 200 and gw.ask(new_key[0]) == 200
    assert gw.ask(gw.alice) == 401
    ledgers = [list(read_events(p)) for p in gw.ledgers.glob("*.jsonl")]
    by_key = {
        events[0].attrs["principal.key_id"]: sum(e.kind is Kind.MODEL_REQUEST for e in events)
        for events in ledgers
    }
    assert sorted(by_key.values()) == [1, 2]  # the straggler alone; the new key's two


def test_an_upstream_change_applies_to_the_next_request(gw: Running) -> None:
    hosts: list[str] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        return httpx2.Response(200, json=REPLY)

    gw.app.state.transport = httpx2.MockTransport(record)
    assert gw.ask(gw.alice) == 200
    gw.edit("https://api.anthropic.com", "https://llm-proxy.corp.example")
    assert gw.reloader.check()
    assert gw.ask(gw.alice) == 200
    assert hosts == ["api.anthropic.com", "llm-proxy.corp.example"]


def test_a_missing_file_is_reported_once(gw: Running, caplog: pytest.LogCaptureFixture) -> None:
    good = gw.path.read_text()
    gw.path.unlink()
    with caplog.at_level(logging.ERROR, logger="seatbelt.gateway.reload"):
        assert not gw.reloader.check()
        assert not gw.reloader.check()
    assert len([r for r in caplog.records if "not reloaded" in r.message]) == 1
    gw.path.write_text(good)
    assert gw.reloader.check()  # back: applied, even with the content it had before


TOOL_USE: dict[str, Any] = {
    **REPLY,
    "content": [{"type": "tool_use", "id": "toolu_1", "name": "run_shell", "input": {}}],
    "stop_reason": "tool_use",
}


def test_relaxing_tools_denied_releases_results_already_refused(gw: Running) -> None:
    gw.edit("session_idle: 900\n", "session_idle: 900\npolicy:\n  tools_denied: [run_shell]\n")
    assert gw.reloader.check()
    gw.app.state.transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json=TOOL_USE))
    assert gw.ask(gw.alice) == 200  # the model asks for run_shell: recorded as denied
    result = {
        "model": "claude-sonnet-5",
        "max_tokens": 10,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": TOOL_USE["content"]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}],
            },
        ],
    }

    def send() -> int:
        headers = {"x-api-key": gw.alice}
        return gw.client.post("/v1/messages", json=result, headers=headers).status_code

    assert send() == 403
    gw.edit("tools_denied: [run_shell]", "tools_denied: [other_tool]")
    assert gw.reloader.check()
    gw.app.state.transport = _upstream()
    assert send() == 200  # the same session, no longer denied


def _status(client: TestClient, key: str) -> int:
    return client.post("/v1/messages", json={"model": "m"}, headers={"x-api-key": key}).status_code


def _until_accepted(client: TestClient, key: str) -> int:
    for _ in range(200):
        if (status := _status(client, key)) != 401:
            return status
        threading.Event().wait(0.01)
    return 401


def _serving(tmp_path: Path) -> tuple[Path, str]:
    keygen(tmp_path / "keys")
    path = tmp_path / "gateway.yaml"
    path.write_text("signing_key: keys/seatbelt.key\n" + BASE)
    return path, add_principal(path, "alice@corp")


@pytest.fixture
def sighup() -> Iterator[list[int]]:
    """A harmless SIGHUP handler for serve() to replace and put back. It counts the signals
    that reach it, so a serve() that failed to install its own cannot kill the test run."""
    hits: list[int] = []
    previous = signal.signal(signal.SIGHUP, lambda signum, _: hits.append(signum))
    try:
        yield hits
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_serve_reloads_on_change_and_on_sighup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sighup: list[int]
) -> None:
    path, alice = _serving(tmp_path)
    ours = signal.getsignal(signal.SIGHUP)
    monkeypatch.setattr(serve_mod, "RELOAD_EVERY", 0.02)
    statuses: list[int] = []
    forced: list[bool] = []

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
            before = app.state.live
            os.kill(os.getpid(), signal.SIGHUP)  # the file has not changed: reloads anyway
            for _attempt in range(200):
                if app.state.live is not before:
                    break
                threading.Event().wait(0.01)
            forced.append(app.state.live is not before)

    monkeypatch.setattr(serve_mod.uvicorn, "run", fake_run)
    serve_mod.serve(path)
    assert statuses == [200, 401, 200, 200] and forced == [True]
    assert sighup == []  # serve()'s handler took both signals
    assert signal.getsignal(signal.SIGHUP) is ours  # and put the previous handler back


def test_a_change_made_while_starting_is_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sighup: list[int]
) -> None:
    """The startup scan of old ledgers can take seconds; a revocation made then must apply."""
    path, alice = _serving(tmp_path)
    scan = serve_mod.close_open_chains
    monkeypatch.setattr(serve_mod, "RELOAD_EVERY", 0.02)

    def slow_scan(root: Path, signer: Signer | None) -> list[Path]:
        _drop(path, "alice@corp")  # the operator revokes alice while the gateway starts
        return scan(root, signer)

    statuses: list[int] = []

    def fake_run(app: Starlette, **_: object) -> None:
        app.state.transport = _upstream()
        with TestClient(app) as client:
            for _attempt in range(200):
                if _status(client, alice) == 401:
                    break
                threading.Event().wait(0.01)
            statuses.append(_status(client, alice))

    monkeypatch.setattr(serve_mod, "close_open_chains", slow_scan)
    monkeypatch.setattr(serve_mod.uvicorn, "run", fake_run)
    serve_mod.serve(path)
    assert statuses == [401]


def test_the_watcher_survives_a_failed_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sighup: list[int]
) -> None:
    path, _ = _serving(tmp_path)
    monkeypatch.setattr(serve_mod, "RELOAD_EVERY", 0.02)
    check = Reloader.check
    failures: list[int] = []

    def flaky(self: Reloader, force: bool = False) -> bool:
        if not failures:
            failures.append(1)
            raise RuntimeError("boom")
        return check(self, force)

    monkeypatch.setattr(Reloader, "check", flaky)
    statuses: list[int] = []

    def fake_run(app: Starlette, **_: object) -> None:
        app.state.transport = _upstream()
        with TestClient(app) as client:
            threading.Event().wait(0.1)  # the first check has raised
            statuses.append(_until_accepted(client, add_principal(path, "bob@corp")))

    monkeypatch.setattr(serve_mod.uvicorn, "run", fake_run)
    serve_mod.serve(path)
    assert failures == [1] and statuses == [200]
