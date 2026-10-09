"""`seatbelt erase`: whole ledgers, removed inside a signed record, with nothing writing."""

# the importer test reuses test_compliance's fake-API helpers
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from seatbelt.attest.sign import Signer, keygen
from seatbelt.cli import app
from seatbelt.erase import ERASED, PREFIX, erase, id_hash, plan, read_erased
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
from seatbelt.locks import RUNNING, Busy, hold_folder
from seatbelt.record.recorder import Recorder
from seatbelt.report import page_path
from seatbelt.report.fleet import fleet
from seatbelt.report.html import write_page
from seatbelt.verify.attest import Attestation, verify_attestation
from seatbelt.verify.chain import verify_file

ALICE = "auth0|alice01"  # as an OIDC subject; slugged into the gateway's run ids
ALICE_MAIL = "alice@corp.example"
ALICE_ANTHROPIC = "user_01Alice"


def _ledger(root: Path, run_id: str, principal: str | None, signer: Signer, **meta: Any) -> Path:
    with Recorder.start(
        root,
        "gateway",
        run_id=run_id,
        metadata={**({"principal.id": principal} if principal else {}), **meta},
        signer=signer,
    ) as rec:
        rec.user_message(principal or "x", "hello from " + (principal or "nobody"))
    return root / f"{run_id}.jsonl"


def _ship(root: Path, ledger: Path) -> None:
    marks = root / ".shipped"
    marks.mkdir(exist_ok=True)
    keys = {f"runs/{ledger.name}": hashlib.sha256(ledger.read_bytes()).hexdigest()}
    (marks / ledger.stem).write_text(json.dumps(keys))


def _build(tmp: Path) -> tuple[Path, Path, Signer]:
    keygen(tmp / "keys")
    signer = Signer.from_file(tmp / "keys" / "seatbelt.key")
    runs = tmp / "runs"
    a1 = _ledger(runs, "auth0_alice01-a-1111", ALICE, signer, **{"principal.name": ALICE_MAIL})
    _ledger(runs, "auth0_alice01-b-2222", ALICE, signer)
    _ship(runs, a1)
    _ledger(runs, "bob-a-3333", "bob@corp", signer)
    _ledger(runs, "anon-4444", None, signer)
    _ledger(runs, "unknown-5555", "unknown", signer)
    Ledger(runs / "auth0_alice01-c-6666.jsonl", "auth0_alice01-c-6666").append(  # died open
        Kind.RUN_START, Actor(type=ActorType.SYSTEM, id="seatbelt"), {"principal.id": ALICE}
    )
    imported = runs / "compliance"
    _ledger(
        imported,
        "cowork-clls_01-1",
        ALICE_ANTHROPIC,
        signer,
        **{"compliance.id": "clls_01", "principal.name": ALICE_MAIL},
    )
    _ledger(imported, "cowork-clls_02-1", "user_01Bob", signer, **{"compliance.id": "clls_02"})
    state = {
        "conversations": {
            "local_session:clls_01": {
                "meta": {"user": {"id": ALICE_ANTHROPIC, "email_address": ALICE_MAIL}}
            },
            "local_session:clls_02": {"meta": {"user": {"id": "user_01Bob"}}},
        }
    }
    (imported / ".state.json").write_text(json.dumps(state))
    return runs, imported, signer


def _all_bytes(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file())


def test_plan_finds_only_the_persons_ledgers(tmp_path: Path) -> None:
    runs, imported, _ = _build(tmp_path)
    p = plan(runs, [ALICE])
    assert sorted(t.ledger.name for t in p.targets) == [
        "auth0_alice01-a-1111.jsonl",
        "auth0_alice01-b-2222.jsonl",
        "auth0_alice01-c-6666.jsonl",  # open, from a process that died
    ]
    shipped = next(t for t in p.targets if t.ledger.name.startswith("auth0_alice01-a"))
    assert shipped.object_keys == ["runs/auth0_alice01-a-1111.jsonl"]
    assert sorted(p.unmatched) == ["anon-4444.jsonl", "unknown-5555.jsonl"]
    assert not p.importer
    q = plan(imported, [ALICE_ANTHROPIC])
    assert [t.ledger.name for t in q.targets] == ["cowork-clls_01-1.jsonl"]
    assert q.state_entries == ["local_session:clls_01"] and q.importer


def test_erase_removes_whole_ledgers_inside_a_signed_record(tmp_path: Path) -> None:
    runs, imported, signer = _build(tmp_path)
    ids = [ALICE, ALICE_ANTHROPIC]
    r = erase(runs, ids, "DSR-2026-014", signer, by="operator")
    s = erase(imported, ids, "DSR-2026-014", signer, by="operator")
    assert (r.ledgers, r.sidecars, r.marks) == (3, 2, 1)  # the open one had no signature
    assert (s.ledgers, s.state_entries) == (1, 1)
    remaining = sorted(p.name for p in runs.glob("*.jsonl") if not p.name.startswith(PREFIX))
    assert remaining == ["anon-4444.jsonl", "bob-a-3333.jsonl", "unknown-5555.jsonl"]
    assert [p.name for p in imported.glob("cowork-*.jsonl")] == ["cowork-clls_02-1.jsonl"]
    # the record verifies, is signed, and names hashes only
    assert r.record is not None and r.record.name.startswith(PREFIX)
    assert verify_file(r.record).ok
    pub = tmp_path / "keys" / "seatbelt.pub"
    assert verify_attestation(r.record, pub).status is Attestation.ATTESTED
    events = list(read_events(r.record))
    assert events[0].attrs["principal.id"] == "seatbelt:erasure"
    assert events[0].attrs["erasure.case"] == "DSR-2026-014"
    actions = [e for e in events if e.kind is Kind.ACTION]
    assert len(actions) == 3 and all(len(a.attrs["erasure.ledger_sha256"]) == 64 for a in actions)
    assert events[-2].attrs["erasure.ledgers"] == 3
    # nothing anywhere in either folder names the person any more
    for folder in (runs, imported):
        data = _all_bytes(folder)
        for needle in (ALICE, "alice01", ALICE_MAIL, ALICE_ANTHROPIC):
            assert needle.encode() not in data, (folder, needle)
    assert read_erased(imported) == {id_hash(ALICE), id_hash(ALICE_ANTHROPIC)}
    report = fleet([runs, imported], pub)
    assert ALICE not in report.by_principal and ALICE_ANTHROPIC not in report.by_principal
    assert report.by_principal["seatbelt:erasure"].runs == 2
    assert not report.forged and not report.broken


def test_erase_removes_the_persons_pages_and_no_one_elses(tmp_path: Path) -> None:
    """A page holds what its ledger holds, so it goes with the ledger."""
    runs, _, signer = _build(tmp_path)
    for ledger in runs.glob("*.jsonl"):
        write_page(ledger)
    p = plan(runs, [ALICE])
    assert sorted(t.page.name for t in p.targets if t.page is not None) == [
        "auth0_alice01-a-1111.html",
        "auth0_alice01-b-2222.html",
        "auth0_alice01-c-6666.html",  # open, so shown as incomplete, and still theirs
    ]
    r = erase(runs, [ALICE], "DSR-2026-015", signer, by="operator")
    assert (r.ledgers, r.pages) == (3, 3)
    assert sorted(p.name for p in runs.glob("*.html")) == [
        "anon-4444.html",
        "bob-a-3333.html",
        "unknown-5555.html",
    ]
    assert r.record is not None and list(read_events(r.record))[-2].attrs["erasure.pages"] == 3
    data = b"".join(f.read_bytes() for f in runs.iterdir() if f.is_file())  # not compliance/
    for needle in (ALICE, "alice01", ALICE_MAIL):
        assert needle.encode() not in data, needle
    assert page_path(runs / "bob-a-3333.jsonl").exists()


def test_an_erase_that_fails_partway_is_finished_by_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, _, signer = _build(tmp_path)
    real = Path.unlink
    calls = {"n": 0}

    def flaky(self: Path, missing_ok: bool = False) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk went away")
        real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky)
    with pytest.raises(OSError):
        erase(runs, [ALICE], "DSR-1", signer, by="op")
    monkeypatch.setattr(Path, "unlink", real)
    (failed,) = runs.glob(f"{PREFIX}*.jsonl")  # closed as failed, and still signed
    assert list(read_events(failed))[-1].attrs["run.ok"] is False
    pub = tmp_path / "keys" / "seatbelt.pub"
    assert verify_attestation(failed, pub).status is Attestation.ATTESTED
    assert list(runs.glob("auth0_alice01*"))  # stopped partway
    # a later erase, even for someone else, finds what the first record named by its hashes
    erase(runs, ["bob@corp"], "DSR-2", signer, by="op")
    assert not list(runs.glob("auth0_alice01*"))
    assert not list((runs / ".shipped").glob("auth0_alice01*"))
    assert not (runs / "bob-a-3333.jsonl").exists()


def test_a_killed_erase_is_closed_and_finished_by_the_next(tmp_path: Path) -> None:
    """Killed with the record open: nothing else closes it while the folder is locked, and
    the next erase closes it, signs it and removes what it named."""
    runs, _, signer = _build(tmp_path)
    target = runs / "auth0_alice01-b-2222.jsonl"
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    killed = Ledger(runs / f"{PREFIX}20260929T000000Z-00000000.jsonl", "erasure-killed")
    agent = Actor(type=ActorType.AGENT, id="seatbelt-erase")
    killed.append(Kind.RUN_START, agent, {"principal.id": "seatbelt:erasure"})
    killed.append(Kind.ACTION, agent, {"erasure.ledger_sha256": digest})
    r = erase(runs, ["nobody@corp"], "DSR-6", signer, by="op")
    assert r.closed == [killed.path.stem]
    assert list(read_events(killed.path))[-1].attrs["run.error"] == "erase interrupted"
    pub = tmp_path / "keys" / "seatbelt.pub"
    assert verify_attestation(killed.path, pub).status is Attestation.ATTESTED
    assert not target.exists() and not (runs / "auth0_alice01-b-2222.attest.json").exists()
    assert (runs / "auth0_alice01-a-1111.jsonl").exists()  # not named: left alone


def test_nothing_to_erase_writes_no_record(tmp_path: Path) -> None:
    runs, _, signer = _build(tmp_path)
    before = sorted(p.name for p in runs.iterdir())
    r = erase(runs, ["nobody@corp"], "DSR-3", signer, by="op")
    assert r.record is None and sorted(p.name for p in runs.iterdir()) == before


def test_a_held_folder_or_a_live_local_run_refuses(tmp_path: Path) -> None:
    runs, _, _ = _build(tmp_path)
    runner = CliRunner()
    args = ["erase", str(runs), "--principal", ALICE, "--case", "DSR-4"]
    args += ["--key", str(tmp_path / "keys" / "seatbelt.key"), "--yes"]
    with hold_folder(runs, "the gateway"):
        out = runner.invoke(app, args)
    assert out.exit_code == 1 and "in use" in out.output
    assert (runs / "auth0_alice01-a-1111.jsonl").exists()
    (runs / RUNNING).mkdir()
    with _held(runs / RUNNING / "r1.lock"):
        out = runner.invoke(app, args)
    assert out.exit_code == 1 and "local runs are recording" in out.output
    assert (runs / "auth0_alice01-a-1111.jsonl").exists()
    with pytest.raises(Busy), hold_folder(runs, "a"), hold_folder(runs, "b"):
        pass


class _held:
    """Hold an OS lock on `path`, as a live local run holds its `.running/<run>.lock`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> None:
        from seatbelt.locks import try_lock

        self.handle = self.path.open("wb")
        assert try_lock(self.handle)

    def __exit__(self, *exc: object) -> None:
        self.handle.close()


def test_the_cli_lists_by_default_and_erases_with_yes(tmp_path: Path) -> None:
    runs, imported, _ = _build(tmp_path)
    people = tmp_path / "people.yaml"
    people.write_text(f"people:\n  Alice:\n    - '{ALICE}'\n    - {ALICE_ANTHROPIC}\n")
    cfg = tmp_path / "g.yaml"
    cfg.write_text(
        f"signing_key: keys/seatbelt.key\nledgers: runs\nupstreams: {{}}\n"
        f"principals:\n  - id: '{ALICE}'\n    key_sha256: '{'0' * 64}'\n    issued: 2026-09-01\n"
    )
    runner = CliRunner()
    base = ["erase", str(runs), str(imported), "--person", "Alice", "--people", str(people)]
    base += ["--case", "DSR-5", "--config", str(cfg)]
    before = _all_bytes(runs)
    out = runner.invoke(app, base)
    assert out.exit_code == 0, out.output
    assert "auth0_alice01-a-1111.jsonl" in out.output and "not deleted by erase" in out.output
    assert f"remove the key issued to {ALICE}" in out.output
    assert "Nothing changed" in out.output and _all_bytes(runs) == before
    out = runner.invoke(app, [*base, "--yes"])
    assert out.exit_code == 0, out.output
    assert "erased 3 ledgers" in out.output and "erased 1 ledgers" in out.output
    assert not list(runs.glob("auth0_alice01*"))
    out = runner.invoke(app, ["erase", str(runs), "--case", "x"])
    assert out.exit_code == 1 and "--principal" in out.output


def test_the_importer_never_brings_an_erased_person_back(tmp_path: Path) -> None:
    from tests.fake_compliance import FakeCompliance, at
    from tests.unit.test_compliance import (
        Clock,
        _importer,
        _local_meta,
        _transcript,
    )

    fake = FakeCompliance()
    fake.local["clls_01"] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    folder = tmp_path / "compliance"
    folder.mkdir()
    (folder / ERASED).write_text(id_hash("user_01Gp") + "\n")  # the fake's owner
    summary = _importer(tmp_path, fake, Clock(120)).run()
    assert (
        summary.written == []
        and "local_session:clls_01"
        not in json.loads((folder / ".state.json").read_text())["conversations"]
    )
    (folder / ERASED).write_text("not a hash\n")
    with pytest.raises(ValueError, match="refusing to import"):
        _importer(tmp_path, fake, Clock(120)).run()


def test_gateway_serve_holds_its_folder(tmp_path: Path) -> None:
    from seatbelt.gateway.serve import serve

    cfg = tmp_path / "g.yaml"
    cfg.write_text("ledgers: runs\nupstreams: {}\n")
    with hold_folder(tmp_path / "runs", "erase"), pytest.raises(Busy, match="the gateway"):
        serve(cfg)
