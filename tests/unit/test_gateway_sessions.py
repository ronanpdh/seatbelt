import threading
from pathlib import Path

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer
from seatbelt.gateway.sessions import Sessions, close_open_chains
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.verify.chain import verify_file


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_same_principal_continues_until_idle(tmp_path: Path) -> None:
    clock = Clock()
    sessions = Sessions(tmp_path, Signer.generate(), idle=60, clock=clock)
    a = sessions.get("alice@corp", None, {"client.ip": "1.2.3.4"})
    sessions.release(a)
    clock.now += 30
    assert sessions.get("alice@corp", None, {}) is a
    sessions.release(a)
    clock.now += 61
    assert sessions.sweep() == 1
    b = sessions.get("alice@corp", None, {})
    assert b is not a
    sessions.release(b)
    sessions.close_all()
    ledgers = sorted(tmp_path.glob("*.jsonl"))
    assert len(ledgers) == 2 and all(verify_file(p).complete for p in ledgers)
    assert all(sidecar(p).exists() for p in ledgers)
    start = next(read_events(a.rec.ledger.path))
    assert start.attrs["principal.id"] == "alice@corp" and start.attrs["client.ip"] == "1.2.3.4"


def test_client_metadata_cannot_override_the_principal(tmp_path: Path) -> None:
    sessions = Sessions(tmp_path, None, idle=60)
    s = sessions.get("alice@corp", "job", {"principal.id": "bob@corp", "run.name": "other"})
    sessions.release(s)
    sessions.close_all()
    start = next(read_events(s.rec.ledger.path))
    assert start.attrs["principal.id"] == "alice@corp" and start.attrs["run.name"] == "job"


def test_named_runs_are_separate_and_end_on_request(tmp_path: Path) -> None:
    sessions = Sessions(tmp_path, None, idle=60)
    a = sessions.get("alice@corp", "job-1", {})
    b = sessions.get("alice@corp", None, {})
    assert a is not b
    sessions.release(a)
    sessions.release(b)
    assert sessions.end("alice@corp", "job-1") is True
    assert sessions.end("alice@corp", "job-1") is False
    sessions.close_all()
    assert len(list(tmp_path.glob("*.jsonl"))) == 2


def test_run_ids_are_safe_filenames(tmp_path: Path) -> None:
    sessions = Sessions(tmp_path, None, idle=60)
    s = sessions.get("alice.o'neil@corp", "a b/../c", {})
    assert "/" not in s.rec.run_id and " " not in s.rec.run_id and ".." not in s.rec.run_id
    sessions.release(s)
    sessions.close_all()


def test_close_open_chains_on_startup(tmp_path: Path) -> None:
    sessions = Sessions(tmp_path, None, idle=60)
    s = sessions.get("alice@corp", None, {})
    s.rec.user_message("u", "hello")
    path = s.rec.ledger.path  # simulate a crash: never closed
    signer = Signer.generate()
    closed = close_open_chains(tmp_path, signer)
    assert closed == [path]
    events = list(read_events(path))
    assert events[-1].kind == Kind.RUN_END and events[-1].attrs["run.ok"] is False
    assert "restart" in events[-1].attrs["run.error"] and sidecar(path).exists()
    assert verify_file(path).complete
    assert close_open_chains(tmp_path, signer) == []


def _last_kind(path: Path) -> Kind:
    return list(read_events(path))[-1].kind


def test_sweep_skips_a_session_with_a_request_in_flight(tmp_path: Path) -> None:
    clock = Clock()
    sessions = Sessions(tmp_path, None, idle=60, clock=clock)
    s = sessions.get("alice@corp", None, {})
    clock.now += 600  # upstream slower than the idle window
    assert sessions.sweep() == 0
    sessions.release(s)  # release counts as activity
    clock.now += 30
    assert sessions.sweep() == 0
    clock.now += 31
    assert sessions.sweep() == 1
    assert verify_file(s.rec.ledger.path).complete


def test_end_while_busy_closes_at_the_last_release(tmp_path: Path) -> None:
    sessions = Sessions(tmp_path, None, idle=60)
    s = sessions.get("alice@corp", "job", {})
    call = s.rec.model_requested("m", {"messages": []})
    assert sessions.end("alice@corp", "job") is True
    assert _last_kind(s.rec.ledger.path) is not Kind.RUN_END  # response still to come
    fresh = sessions.get("alice@corp", "job", {})
    assert fresh is not s  # an ended run is never reused
    call.respond({"content": "ok"})
    sessions.release(s)
    assert _last_kind(s.rec.ledger.path) is Kind.RUN_END
    assert verify_file(s.rec.ledger.path).complete
    sessions.release(fresh)
    assert sessions.close_all() == 0


def test_close_all_waits_for_in_flight_requests(tmp_path: Path) -> None:
    sessions = Sessions(tmp_path, None, idle=60)
    s = sessions.get("alice@corp", None, {})
    timer = threading.Timer(0.05, lambda: sessions.release(s))
    timer.start()
    assert sessions.close_all(timeout=5) == 0
    timer.join()
    assert verify_file(s.rec.ledger.path).complete


def test_close_all_gives_up_after_timeout_and_restart_recovers(tmp_path: Path) -> None:
    sessions = Sessions(tmp_path, None, idle=60)
    s = sessions.get("alice@corp", None, {})
    assert sessions.close_all(timeout=0.01) == 1
    assert close_open_chains(tmp_path, None) == [s.rec.ledger.path]
