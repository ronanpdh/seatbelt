"""Regressions from the review of the importer, `erase` and `report`: IER-1 to IER-10."""

# the importer tests reuse test_compliance's fake-API helpers
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import io
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from tests.fake_compliance import FakeCompliance, at, error
from tests.unit.test_compliance import (
    Clock,
    _chat,
    _chat_messages,
    _importer,
    _ledgers,
    _local_meta,
    _transcript,
)
from typer.testing import CliRunner

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, keygen
from seatbelt.cli import app
from seatbelt.compliance.client import MAX_ATTEMPTS, backoff
from seatbelt.compliance.importer import Summary
from seatbelt.erase import ERASED, erase, id_hash, plan
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
from seatbelt.record.recorder import Recorder
from seatbelt.report.fleet import fleet
from seatbelt.report.timeline import timeline

ESC = "\x1b"


def _keys(tmp: Path) -> tuple[Signer, Path]:
    keygen(tmp / "keys")
    return Signer.from_file(tmp / "keys" / "seatbelt.key"), tmp / "keys" / "seatbelt.pub"


def _run(
    runs: Path,
    run_id: str,
    principal: str,
    signer: Signer | None,
    tokens: int = 5,
    model: str = "claude-opus-5-5",
    tool: str = "lookup",
) -> Path:
    with Recorder.start(
        runs, "gateway", run_id=run_id, metadata={"principal.id": principal}, signer=signer
    ) as rec:
        call = rec.model_requested(model, {"messages": []})
        call.respond({}, {"input_tokens": tokens, "output_tokens": 1}, response_model=model)
        rec.tool_called(tool, {"q": 1})
    return runs / f"{run_id}.jsonl"


def _open(runs: Path, run_id: str, principal: str) -> None:
    ledger = Ledger(runs / f"{run_id}.jsonl", run_id)
    ledger.append(
        Kind.RUN_START, Actor(type=ActorType.SYSTEM, id="seatbelt"), {"principal.id": principal}
    )
    ledger.append(Kind.USER_MESSAGE, Actor(type=ActorType.USER, id=principal), {})


# -- IER-1: an emptied ledger, a signature left without its ledger -------------------------------


def test_an_emptied_signed_ledger_is_forged_or_broken_and_fails_the_report(tmp_path: Path) -> None:
    signer, pub = _keys(tmp_path)
    runs = tmp_path / "runs"
    _run(runs, "alice-1", "alice@corp", signer)
    _run(runs, "bob-1", "bob@corp", signer).write_bytes(b"")  # emptied; signature kept
    report = fleet(runs, pub)
    assert report.forged == ["bob-1"] and "bob@corp" not in report.by_principal
    assert fleet(runs).broken == ["bob-1"]  # no key: the leftover signature still shows it
    out = CliRunner().invoke(app, ["report", str(runs)])
    assert out.exit_code == 1 and "broken: 1 (bob-1)" in out.output


def test_an_empty_ledger_with_no_signature_is_listed_as_incomplete(tmp_path: Path) -> None:
    """A crash between creating the file and writing its first event."""
    signer, pub = _keys(tmp_path)
    runs = tmp_path / "runs"
    _run(runs, "alice-1", "alice@corp", signer)
    (runs / "carol-1.jsonl").write_bytes(b"")
    report = fleet(runs, pub)
    assert report.incomplete == ["carol-1"] and not report.broken and not report.forged
    assert report.runs == 1


def test_a_signature_with_no_ledger_is_missing_and_fails_the_report(tmp_path: Path) -> None:
    signer, pub = _keys(tmp_path)
    runs = tmp_path / "runs"
    _run(runs, "alice-1", "alice@corp", signer)
    _run(runs, "bob-1", "bob@corp", signer).unlink()  # its signature left behind
    assert fleet(runs, pub).missing == ["bob-1"]
    out = CliRunner().invoke(app, ["report", str(runs), "--pubkey", str(pub)])
    assert out.exit_code == 1 and "missing: 1 (bob-1)" in out.output


def test_a_signature_left_by_an_interrupted_erase_is_not_missing(tmp_path: Path) -> None:
    signer, pub = _keys(tmp_path)
    runs = tmp_path / "runs"
    ledger = _run(runs, "bob-1", "bob@corp", signer)
    side = sidecar(ledger)
    with Recorder.start(
        runs,
        "seatbelt-erase",
        run_id="erasure-20260929T000000Z-00000000",
        metadata={"principal.id": "seatbelt:erasure"},
        signer=signer,
    ) as rec:
        digest = hashlib.sha256(ledger.read_bytes()).hexdigest()
        rec.action(
            "erase ledger",
            f"sha256:{digest}",
            attrs={
                "erasure.ledger_sha256": digest,
                "erasure.sidecar_sha256": hashlib.sha256(side.read_bytes()).hexdigest(),
            },
        )
    ledger.unlink()  # erase removes the ledger first, then its signature: killed between
    report = fleet(runs, pub)
    assert report.missing == [] and not report.forged and not report.broken


# -- IER-2: forged and closed-but-unsigned ledgers are not summed --------------------------------


def test_a_forged_ledger_is_not_counted(tmp_path: Path) -> None:
    signer, pub = _keys(tmp_path)
    runs = tmp_path / "runs"
    _run(runs, "alice-1", "alice@corp", signer)
    bob = _run(runs, "bob-1", "bob@corp", signer)
    side = sidecar(bob)
    manifest = json.loads(side.read_text())
    manifest["events"] += 1
    side.write_text(json.dumps(manifest))
    report = fleet(runs, pub)
    assert report.forged == ["bob-1"] and "bob@corp" not in report.by_principal
    assert report.runs == 1 and report.by_model["claude-opus-5-5"].input_tokens == 5
    assert report.by_tool == {"lookup": 1}


def test_with_a_key_an_ended_but_unsigned_ledger_is_not_counted_and_fails(tmp_path: Path) -> None:
    signer, pub = _keys(tmp_path)
    runs = tmp_path / "runs"
    _run(runs, "alice-1", "alice@corp", signer)
    _run(runs, "carol-1", "carol@corp", None, tokens=999999)  # re-chained, signature deleted
    _open(runs, "dave-1", "dave@corp")  # live: signed only when it ends
    report = fleet(runs, pub)
    assert report.unsigned == ["carol-1"] and "carol@corp" not in report.by_principal
    assert report.by_principal["dave@corp"].runs == 1 and report.incomplete == ["dave-1"]
    assert report.unattested == ["dave-1"]
    out = CliRunner().invoke(app, ["report", str(runs), "--pubkey", str(pub)])
    assert out.exit_code == 1 and "ended but unsigned: 1 (carol-1)" in out.output
    # without a key nothing can be checked: counted, and listed as unattested
    assert fleet(runs).by_principal["carol@corp"].input_tokens == 999999
    assert CliRunner().invoke(app, ["report", str(runs)]).exit_code == 0


def test_with_a_key_an_open_ledger_alone_still_passes(tmp_path: Path) -> None:
    signer, pub = _keys(tmp_path)
    runs = tmp_path / "runs"
    _run(runs, "alice-1", "alice@corp", signer)
    _open(runs, "dave-1", "dave@corp")
    out = CliRunner().invoke(app, ["report", str(runs), "--pubkey", str(pub)])
    assert out.exit_code == 0, out.output


# -- IER-3: ledger text never reaches the terminal as control sequences --------------------------


def test_the_timeline_escapes_control_sequences_even_where_it_cuts(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    with Recorder.start(
        runs, "gateway", run_id="r1", metadata={"principal.id": "u"}, signer=None
    ) as rec:
        # an OSC opener near the 80-character cut, whose terminator falls after it
        rec.user_message("u", "a" * 70 + f"{ESC}]52;c;aGk={ESC}\\" + "b" * 20)
        rec.tool_called(f"bash{ESC}[1A{ESC}[2K", {"cmd": f"{ESC}[31mls"})
    buf = io.StringIO()
    timeline(runs / "r1.jsonl", Console(file=buf, width=500))
    out = buf.getvalue()
    assert ESC not in out
    assert "bash\\x1b[1A\\x1b[2K" in out and "a" * 70 + "\\x1b]52" in out


def test_the_report_escapes_control_sequences(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "m-1", f"mallory{ESC}[2K", None, model=f"x{ESC}]52;c;aGk={ESC}\\", tool=f"t{ESC}[1A")
    (runs / f"broken{ESC}[2K.jsonl").write_text("not json\n")
    people = tmp_path / "people.yaml"
    people.write_text('people:\n  "Eve\\e[8m":\n    - "mallory\\e[2K"\n')  # YAML: \e is ESC
    out = CliRunner().invoke(app, ["report", str(runs), "--people", str(people)])
    assert ESC not in out.output
    assert "Eve\\x1b[8m" in out.output and "t\\x1b[1A" in out.output
    assert "x\\x1b]52" in out.output and "broken\\x1b[2K" in out.output


def test_runs_escapes_control_sequences_in_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from seatbelt.gateway import launcher

    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(launcher, "LEGACY_CONFIG", tmp_path / "gateway.toml")
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    runs = tmp_path / "home" / "runs"
    with Recorder.start(runs, "local", run_id="r1", metadata={"run.name": f"n{ESC}[2K"}) as rec:
        rec.user_message("u", "hi")
    (runs / f"torn{ESC}[1A.jsonl").write_text("{")
    out = CliRunner().invoke(app, ["runs"])
    assert out.exit_code == 0, out.output
    assert ESC not in out.output and "n\\x1b[2K" in out.output and "torn\\x1b[1A" in out.output


def test_the_erase_listing_escapes_file_names(tmp_path: Path) -> None:
    signer, _ = _keys(tmp_path)
    runs = tmp_path / "runs"
    _run(runs, "alice-1", "alice@corp", signer).rename(runs / f"alice{ESC}[2K-1.jsonl")
    out = CliRunner().invoke(app, ["erase", str(runs), "--principal", "alice@corp", "--case", "X"])
    assert out.exit_code == 0, out.output
    assert ESC not in out.output and "alice\\x1b[2K-1.jsonl" in out.output


# -- IER-4: a pending local session that cannot be read again -----------------------------------


def _unlisted_pending(tmp_path: Path, fake: FakeCompliance, clock: Clock) -> None:
    """Leave clls_01 pending and out of the next list window, so the next run reads it again."""
    fake.local["clls_01"] = {"meta": _local_meta("clls_01", at(100)), "messages": _transcript()}
    for step in (0, 6):  # listed, not settled yet
        clock.advance(step)
        assert _importer(tmp_path, fake, clock, sources=["local_sessions", "chats"]).run().ok
    clock.advance(114)


def _retrieve(request: Any) -> bool:
    return str(request.url.path).endswith("/sessions/local/clls_01")


def test_a_session_that_cannot_be_read_again_is_stuck_after_three_runs(tmp_path: Path) -> None:
    fake = FakeCompliance()
    clock = Clock(110)
    _unlisted_pending(tmp_path, fake, clock)
    for _ in range(3):
        fake.faults.append((_retrieve, error(500, "boom", **{"x-should-retry": "false"})))
    summaries: list[Summary] = []
    for n in range(3):
        # the other sources carry on: a chat that settled meanwhile is imported
        fake.chats[f"c{n}"] = {"meta": _chat(f"c{n}", at(100 + n)), "messages": _chat_messages()}
        summary = _importer(tmp_path, fake, clock, sources=["local_sessions", "chats"]).run()
        assert [p.stem for p in summary.written] == [f"chat-c{n}-1"]
        summaries.append(summary)
        clock.advance(1)
    assert [s.ok for s in summaries] == [True, True, False]
    assert summaries[0].skipped == summaries[1].skipped == ["local_session:clls_01"]
    assert summaries[2].stuck == ["local_session:clls_01"]
    state = json.loads((tmp_path / "compliance" / ".state.json").read_text())
    assert state["conversations"]["local_session:clls_01"]["failures"] == 3
    # readable again: imported, and the count starts over
    summary = _importer(tmp_path, fake, clock, sources=["local_sessions", "chats"]).run()
    assert summary.ok and [p.stem for p in summary.written] == ["cowork-clls_01-1"]
    state = json.loads((tmp_path / "compliance" / ".state.json").read_text())
    assert state["conversations"]["local_session:clls_01"]["failures"] == 0


def test_content_unavailable_on_reading_a_session_again_blocks_its_org(tmp_path: Path) -> None:
    fake = FakeCompliance()
    clock = Clock(110)
    _unlisted_pending(tmp_path, fake, clock)
    captured = error(503, "Captured content could not be decrypted", **{"x-should-retry": "false"})
    fake.faults.append((_retrieve, captured))
    importer = _importer(tmp_path, fake, clock, sources=["local_sessions", "chats"])
    summary = importer.run()
    assert summary.ok and summary.skipped == ["local_session:clls_01"]
    assert importer._blocked_orgs == {"9a1e0000-0000-0000-0000-000000000000"}


# -- IER-5: a segment that failed partway through a message --------------------------------------


def test_a_message_cut_short_by_a_failed_run_is_recorded_again_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCompliance()
    fake.local["clls_01"] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    real = Recorder.tool_called

    def full_disk(*args: Any, **kwargs: Any) -> Any:
        raise OSError("No space left on device")

    monkeypatch.setattr(Recorder, "tool_called", full_disk)  # clsm_2's answer, not its call
    clock = Clock(120)
    with pytest.raises(OSError):
        _importer(tmp_path, fake, clock).run()
    monkeypatch.setattr(Recorder, "tool_called", real)
    first = _ledgers(tmp_path / "compliance")["cowork-clls_01-1"]
    assert first[-1].attrs["run.ok"] is False
    assert first[-2].attrs["compliance.message_id"] == "clsm_2"  # its answer, no tool call
    clock.advance(1)
    _importer(tmp_path, fake, clock).run()
    second = _ledgers(tmp_path / "compliance")["cowork-clls_01-2"]
    ids = [e.attrs.get("compliance.message_id") for e in second[1:-1]]
    assert ids == ["clsm_2", "clsm_2", "clsm_3", "clsm_4"]
    call = next(e for e in second if e.kind is Kind.TOOL_CALL)
    result = next(e for e in second if e.kind is Kind.TOOL_RESULT)
    assert call.attrs["gen_ai.tool.name"] == "Read" and result.parent_id == call.id


# -- IER-6: a chat returned only in part --------------------------------------------------------


def test_a_chat_returned_with_has_more_writes_nothing_and_is_stuck(tmp_path: Path) -> None:
    fake = FakeCompliance()
    fake.chats["c1"] = {"meta": _chat("c1", at(10)), "messages": _chat_messages(), "has_more": True}
    clock = Clock(120)
    summary = _importer(tmp_path, fake, clock, sources=["chats"]).run()
    assert not summary.ok and summary.stuck == ["chat:c1"] and summary.written == []
    assert not list((tmp_path / "compliance").glob("*.jsonl"))
    fake.chats["c1"]["has_more"] = False  # whole again: still pending, so imported
    clock.advance(1)
    summary = _importer(tmp_path, fake, clock, sources=["chats"]).run()
    assert summary.ok and [p.stem for p in summary.written] == ["chat-c1-1"]


# -- IER-8: unreadable ledgers that are, or may be, the person's ---------------------------------


def test_erase_names_and_keeps_unreadable_ledgers_that_may_be_theirs(tmp_path: Path) -> None:
    signer, _ = _keys(tmp_path)
    runs = tmp_path / "runs"
    _run(runs, "alice-1", "alice@corp", signer)
    torn = _run(runs, "alice-2", "alice@corp", signer)
    with torn.open("a") as fh:
        fh.write('{"torn')  # power lost mid-append
    bobs = _run(runs, "bob-1", "bob@corp", signer)
    with bobs.open("a") as fh:
        fh.write('{"torn')
    (runs / "garbage-1.jsonl").write_text("not an event\n")
    p = plan(runs, ["alice@corp"])
    assert p.to_check == ["alice-2.jsonl", "garbage-1.jsonl"]
    assert p.unreadable == ["alice-2.jsonl", "bob-1.jsonl", "garbage-1.jsonl"]
    args = ["erase", str(runs), "--principal", "alice@corp", "--case", "DSR-1"]
    out = CliRunner().invoke(app, [*args, "--key", str(tmp_path / "keys" / "seatbelt.key")])
    assert out.exit_code == 0 and "may be theirs" in out.output  # only listed
    out = CliRunner().invoke(
        app, [*args, "--key", str(tmp_path / "keys" / "seatbelt.key"), "--yes"]
    )
    assert out.exit_code == 1, out.output
    assert str(torn) in out.output and str(runs / "garbage-1.jsonl") in out.output
    assert str(bobs) not in out.output.split("Not erased")[1]
    assert not (runs / "alice-1.jsonl").exists()  # the rest was erased
    assert torn.exists() and (runs / "garbage-1.jsonl").exists() and bobs.exists()


# -- IER-9: the suppression list is the owner's only ---------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_suppression_list_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    signer, _ = _keys(tmp_path)
    folder = tmp_path / "compliance"
    _run(folder, "cowork-a-1", "user_01A", signer)
    (folder / ERASED).write_text(id_hash("user_01Old") + "\n")
    (folder / ERASED).chmod(0o644)
    erase(folder, ["user_01A"], "DSR-1", signer, by="op")
    assert stat.S_IMODE((folder / ERASED).stat().st_mode) == 0o600
    lines = (folder / ERASED).read_text().split()
    assert sorted(lines) == sorted([id_hash("user_01Old"), id_hash("user_01A")])
    assert not list(read_events(next(folder.glob("erasure-*.jsonl"))))[-1].attrs.get("run.error")


# -- IER-10: the retry budget -------------------------------------------------------------------


def test_the_retry_budget_is_about_a_minute() -> None:
    """client.py and the docs state it; waits come between attempts."""
    assert sum(backoff(a) for a in range(MAX_ATTEMPTS - 1)) == 63
