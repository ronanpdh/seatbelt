from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from seatbelt.ledger.events import GENESIS_HASH, SCHEMA_VERSION, Actor, ActorType, Event, Kind
from seatbelt.ledger.redact import redact_text
from seatbelt.ledger.store import Ledger
from seatbelt.verify.chain import verify_events, verify_file

AGENT = Actor(type=ActorType.AGENT, id="a")


def test_hash_is_deterministic() -> None:
    e = Event(
        schema_version=SCHEMA_VERSION,
        run_id="r",
        seq=0,
        kind=Kind.RUN_START,
        actor=AGENT,
        prev_hash=GENESIS_HASH,
    )
    assert e.compute_hash() == e.compute_hash()
    assert e.sealed().hash == e.compute_hash()


def test_chain_links_and_verifies(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "r.jsonl", "r")
    first = ledger.append(Kind.RUN_START, AGENT)
    second = ledger.append(Kind.OUTCOME, AGENT, {"outcome.success": True})
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.hash
    assert verify_file(tmp_path / "r.jsonl").ok


def test_reopening_continues_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    Ledger(path, "r").append(Kind.RUN_START, AGENT)
    reopened = Ledger(path, "r")
    third = reopened.append(Kind.RUN_END, AGENT)
    assert third.seq == 1
    assert verify_file(path).ok


@given(st.text(min_size=1))
def test_editing_any_attr_breaks_the_chain(tampered: str) -> None:
    ledger_events = [
        Event(
            schema_version=SCHEMA_VERSION,
            run_id="r",
            seq=0,
            kind=Kind.RUN_START,
            actor=AGENT,
            prev_hash=GENESIS_HASH,
        ).sealed()
    ]
    original = Event(
        schema_version=SCHEMA_VERSION,
        run_id="r",
        seq=1,
        kind=Kind.OUTCOME,
        actor=AGENT,
        attrs={"outcome.summary": "original"},
        prev_hash=ledger_events[0].hash,
    ).sealed()
    ledger_events.append(original)
    assert verify_events(ledger_events).ok
    if tampered == "original":
        return
    forged = original.model_copy(update={"attrs": {"outcome.summary": tampered}})
    verdict = verify_events([ledger_events[0], forged])
    assert not verdict.ok
    assert verdict.first_bad_seq == 1


def test_deleting_an_event_breaks_the_chain(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "r.jsonl", "r")
    for _ in range(3):
        ledger.append(Kind.ACTION, AGENT)
    lines = (tmp_path / "r.jsonl").read_text().splitlines()
    (tmp_path / "r.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
    verdict = verify_file(tmp_path / "r.jsonl")
    assert not verdict.ok
    assert verdict.first_bad_seq == 2


def test_events_carry_schema_version_and_verify_rejects_unknown(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "r.jsonl", "r")
    event = ledger.append(Kind.RUN_START, AGENT)
    assert event.schema_version == SCHEMA_VERSION
    assert '"schema_version":' in (tmp_path / "r.jsonl").read_text()

    future = event.model_copy(update={"schema_version": SCHEMA_VERSION + 1}).sealed()
    verdict = verify_events([future])
    assert not verdict.ok
    assert verdict.first_bad_seq == 0
    assert verdict.reason is not None
    assert "schema version" in verdict.reason


def test_unversioned_ledger_is_rejected_as_old_format_not_tampering(tmp_path: Path) -> None:
    path = tmp_path / "old.jsonl"
    Ledger(path, "old").append(Kind.RUN_START, AGENT)
    path.write_text(path.read_text().replace('"schema_version":1,', ""))
    verdict = verify_file(path)
    assert not verdict.ok
    assert verdict.reason is not None
    assert "schema_version" in verdict.reason


def test_redaction_catches_keys() -> None:
    assert "sk-ant-" not in redact_text("key sk-ant-abcdefghijklmnopqrstuvwxyz1234")
    assert "AKIA" not in redact_text("AKIAABCDEFGHIJKLMNOP")
    assert redact_text("nothing here") == "nothing here"
