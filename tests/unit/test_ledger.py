import json
import stat
import threading
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from seatbelt.ledger.events import GENESIS_HASH, SCHEMA_VERSION, Actor, ActorType, Event, Kind
from seatbelt.ledger.redact import redact_text
from seatbelt.ledger.store import Ledger
from seatbelt.record.recorder import Recorder
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


def test_concurrent_appends_keep_the_chain_intact(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "r.jsonl", "r")

    def write() -> None:
        for _ in range(200):
            ledger.append(Kind.ACTION, AGENT, {"x": "y" * 50})

    threads = [threading.Thread(target=write) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    verdict = verify_file(tmp_path / "r.jsonl")
    assert verdict.ok, verdict.reason
    assert verdict.events == 1600


def test_redaction_catches_keys() -> None:
    assert "sk-ant-" not in redact_text("key sk-ant-abcdefghijklmnopqrstuvwxyz1234")
    assert "AKIA" not in redact_text("AKIAABCDEFGHIJKLMNOP")
    assert redact_text("nothing here") == "nothing here"


def test_extra_keys_on_a_line_break_verification(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    Ledger(path, "r").append(Kind.RUN_START, AGENT)
    line = json.loads(path.read_text())
    line["reviewer_note"] = "approved"
    line["actor"]["role"] = "admin"
    path.write_text(json.dumps(line) + "\n")
    verdict = verify_file(path)
    assert not verdict.ok
    assert verdict.reason is not None
    assert "extra" in verdict.reason.lower()


def test_complete_requires_exactly_one_run_start_at_seq_zero(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="a", run_id="r"):
        pass
    assert verify_file(tmp_path / "r.jsonl").complete
    ledger = Ledger(tmp_path / "r.jsonl", "r")
    ledger.append(Kind.RUN_START, AGENT)
    ledger.append(Kind.RUN_END, AGENT, {"run.events": 4})
    verdict = verify_file(tmp_path / "r.jsonl")
    assert verdict.ok
    assert not verdict.complete


def test_recorder_refuses_an_existing_ledger(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="a", run_id="r"):
        pass
    with pytest.raises(FileExistsError), Recorder.start(tmp_path, agent_id="a", run_id="r"):
        pass


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "a b", "x\\y"])
def test_recorder_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    with (
        pytest.raises(ValueError, match="run_id"),
        Recorder.start(tmp_path, agent_id="a", run_id=run_id),
    ):
        pass
    assert not list(tmp_path.rglob("*.jsonl"))


def test_ledger_is_private_to_the_writer(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    Ledger(path, "r").append(Kind.RUN_START, AGENT)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-abcdefghijklmnopqrstuvwxyz1234",
        "sk-proj-abcdefghijklmnopqrstuvwxyz1234",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0123456789abcdefghijklmnopqrstuvwxyz0123456789",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",
        "Bearer abc123def456ghi7",
    ],
)
def test_redaction_catches_each_secret_format(secret: str) -> None:
    assert secret not in redact_text(f"token {secret} here")


@pytest.mark.parametrize(
    "text",
    [
        "key_sk-ant-abcdefghijklmnopqrstuvwxyz1234",
        "AKIAABCDEFGHIJKLMNOP_x",
        "x=sk-abcdefghijklmnopqrstuvwxyz1234",
    ],
)
def test_redaction_catches_secrets_glued_to_punctuation(text: str) -> None:
    assert "REDACTED" in redact_text(text)


@given(st.from_regex(r"\A([a-z]{1,12} ){1,20}\Z"))
def test_redaction_leaves_ordinary_prose_alone(prose: str) -> None:
    assert redact_text(prose) == prose


@pytest.mark.parametrize("text", ["the bearer of bad news", "desk-abcdefghijklmnopqrstuvwxyz"])
def test_redaction_does_not_fire_on_word_fragments(text: str) -> None:
    assert redact_text(text) == text
