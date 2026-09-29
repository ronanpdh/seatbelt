"""Regression tests for the ledger and evidence-pack review findings (LC-*, IER-3)."""

import errno
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest
from examples.scenario_target import target
from typer.testing import CliRunner

from seatbelt.attest.manifest import AttestError, sidecar
from seatbelt.attest.sign import Signer, attest, keygen
from seatbelt.cli import app
from seatbelt.ledger import store
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.redact import redact
from seatbelt.ledger.store import Ledger, LedgerError, read_events
from seatbelt.record.recorder import Recorder
from seatbelt.report import pack
from seatbelt.report.pack import MANIFEST, PackError, PackManifest, PackStatus, build, verify_pack
from seatbelt.scenarios.runner import run
from seatbelt.verify.attest import Attestation, verify_attestation
from seatbelt.verify.chain import verify_file

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()
AGENT = Actor(type=ActorType.AGENT, id="a")
KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz1234"
OTHER_KEY = "sk-ant-zyxwvutsrqponmlkjihgfedcba9876"


def _run(root: Path, run_id: str = "r", signer: Signer | None = None) -> Path:
    with Recorder.start(root, agent_id="bot", run_id=run_id, signer=signer) as rec:
        rec.outcome("done", success=True)
    return root / f"{run_id}.jsonl"


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _zip(members: dict[str, bytes], dst: Path) -> Path:
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return dst


def _reseal(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    """Recompute member hashes in a raw (unsigned) manifest and store it as pack.json."""
    manifest["members"] = [
        {"path": p, "sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)}
        for p, d in sorted(members.items())
        if p != MANIFEST
    ]
    manifest["signature"] = manifest["public_key"] = ""
    members[MANIFEST] = json.dumps(manifest).encode()


# -- LC-1: every value that is hashed is also written ------------------------------------


def test_lone_surrogates_are_written_as_visible_escapes(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "r.jsonl", "r")
    actor = Actor(type=ActorType.TOOL, id="t\ud800", version="v\udfff")
    attrs = {"x": "a\ud800b", "k\udc00": [{"\ud83d": "y"}], "k\\udc00": 2}
    event = ledger.append(Kind.TOOL_CALL, actor, attrs, parent_id="p\ud800")
    assert (event.actor.id, event.actor.version, event.parent_id) == (
        "t\\ud800",
        "v\\udfff",
        "p\\ud800",
    )
    assert event.attrs["x"] == "a\\ud800b"
    assert event.attrs["k\\udc00"] == [{"\\ud83d": "y"}]
    assert event.attrs["k\\udc00#2"] == 2  # the key the escape collided with keeps its value
    verdict = verify_file(tmp_path / "r.jsonl")
    assert verdict.ok and verdict.events == 1, verdict.reason
    assert next(read_events(tmp_path / "r.jsonl")) == event


def test_streamed_tool_argument_with_a_lone_surrogate_is_recorded(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec:
        rec.tool_called("note", json.loads('{"x": "\\ud800"}'))
    events = list(read_events(tmp_path / "r.jsonl"))
    assert events[1].attrs["gen_ai.tool.call.arguments"] == {"x": "\\ud800"}
    assert verify_file(tmp_path / "r.jsonl").complete


def test_a_serialisation_error_never_touches_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "r.jsonl"

    def boom(*_: object, **__: object) -> str:
        raise ValueError("cannot serialise")

    monkeypatch.setattr("seatbelt.ledger.events.Event.model_dump_json", boom)
    with pytest.raises(ValueError, match="serialise"):
        Ledger(path, "r").append(Kind.RUN_START, AGENT)
    assert not path.exists()


# -- LC-3: a failed append leaves the file as it was -----------------------------------


def test_failed_fsync_is_rolled_back_and_the_chain_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "r.jsonl"
    ledger = Ledger(path, "r")
    ledger.append(Kind.RUN_START, AGENT)
    before = path.read_bytes()
    real = os.fsync

    def full(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store.os, "fsync", full)
    with pytest.raises(OSError, match="No space"):
        ledger.append(Kind.ACTION, AGENT, {"x": "lost"})
    monkeypatch.setattr(store.os, "fsync", real)
    assert path.read_bytes() == before
    ledger.append(Kind.RUN_END, AGENT, {"run.events": 2})
    verdict = verify_file(path)
    assert verdict.ok and verdict.complete, verdict.reason


def test_short_write_is_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "r.jsonl"
    ledger = Ledger(path, "r")
    ledger.append(Kind.RUN_START, AGENT)
    before = path.read_bytes()
    real = os.write
    calls: list[int] = []

    def torn(fd: int, data: Any) -> int:
        calls.append(fd)
        if len(calls) == 1:
            return real(fd, bytes(data)[:10])
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(store.os, "write", torn)
    with pytest.raises(OSError, match="I/O"):
        ledger.append(Kind.ACTION, AGENT)
    monkeypatch.setattr(store.os, "write", real)
    assert path.read_bytes() == before
    ledger.append(Kind.ACTION, AGENT)
    assert verify_file(path).ok


def test_a_failed_rollback_makes_the_ledger_refuse_further_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(tmp_path / "r.jsonl", "r")
    ledger.append(Kind.RUN_START, AGENT)

    def fail(*_: object) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(store.os, "fsync", fail)
    monkeypatch.setattr(store.os, "ftruncate", fail)
    with pytest.raises(OSError):
        ledger.append(Kind.ACTION, AGENT)
    monkeypatch.undo()
    with pytest.raises(LedgerError, match="could not be undone"):
        ledger.append(Kind.ACTION, AGENT)


# -- LC-9: sidecars and keys are written whole or not at all ---------------------------


def test_new_ledger_file_fsyncs_its_directory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(store, "fsync_dir", synced.append)
    ledger = Ledger(tmp_path / "runs" / "r.jsonl", "r")
    ledger.append(Kind.RUN_START, AGENT)
    ledger.append(Kind.ACTION, AGENT)
    assert synced == [tmp_path / "runs"]


def test_sidecar_that_fails_to_sync_is_never_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _run(tmp_path)

    def fail(fd: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr("seatbelt.attest.sign.os.fsync", fail)
    with pytest.raises(OSError):
        attest(ledger, Signer.generate())
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r.jsonl"]
    attest(ledger, Signer.generate())
    assert verify_attestation(ledger, None).status is Attestation.UNCHECKED
    with pytest.raises(AttestError, match="exists"):
        attest(ledger, Signer.generate())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r.attest.json", "r.jsonl"]


def test_sidecar_written_without_hard_links_still_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _run(tmp_path)

    def no_links(*_: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("seatbelt.attest.sign.os.link", no_links)
    key, pub = keygen(tmp_path / "keys")
    attest(ledger, Signer.from_file(key))
    assert verify_attestation(ledger, pub).status is Attestation.ATTESTED
    assert sorted(p.name for p in (tmp_path / "keys").iterdir()) == ["seatbelt.key", "seatbelt.pub"]


# -- LC-11: dict keys, actor ids and parent ids are redacted ---------------------------


def test_redaction_covers_dict_keys_without_losing_values() -> None:
    out = redact({"env": {KEY: "1", OTHER_KEY: "2", "[REDACTED:anthropic_key]": "3"}})
    assert KEY not in json.dumps(out) and OTHER_KEY not in json.dumps(out)
    assert sorted(out["env"].values()) == ["1", "2", "3"]
    assert redact(out) == out  # idempotent, so the ledger can redact again


def test_ledger_redacts_actor_and_parent_whatever_the_caller(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "r.jsonl", "r")
    actor = Actor(type=ActorType.MODEL, id=f"m {KEY}", version=KEY)
    ledger.append(Kind.MODEL_RESPONSE, actor, {"ok": True}, parent_id=KEY)
    with Recorder.start(tmp_path, agent_id="bot", run_id="rec") as rec:
        with rec.model_call(f"model {KEY}", {"messages": []}) as call:
            call.respond({}, response_model=KEY)
        rec.tool_called("t", {"env": {KEY: "1"}})
    for path in (tmp_path / "r.jsonl", tmp_path / "rec.jsonl"):
        assert KEY not in path.read_text()
        assert verify_file(path).ok


# -- LC-5: a sidecar that contradicts its ledger fails even without a key --------------


def _rechained(tmp_path: Path) -> Path:
    """A signed ledger whose tail was rewritten and re-chained, original sidecar kept."""
    ledger = _run(tmp_path, signer=Signer.generate())
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-2]) + "\n")
    Ledger(ledger, "r").append(Kind.RUN_END, AGENT, {"run.ok": True, "run.events": 2})
    assert verify_file(ledger).complete
    return ledger


def test_mismatched_sidecar_without_a_key_is_forged_not_unchecked(tmp_path: Path) -> None:
    ledger = _rechained(tmp_path)
    verdict = verify_attestation(ledger, None)
    assert verdict.status is Attestation.FORGED
    assert "does not match this ledger" in (verdict.reason or "")
    assert "not checked against a key" in (verdict.reason or "")
    result = runner.invoke(app, ["verify", str(ledger)])
    assert result.exit_code == 1 and "FORGED" in result.output
    with pytest.raises(PackError, match="does not match this ledger"):
        build(tmp_path, tmp_path.parent / "p.zip")


def test_matching_sidecar_without_a_key_stays_unchecked(tmp_path: Path) -> None:
    ledger = _run(tmp_path, signer=Signer.generate())
    assert verify_attestation(ledger, None).status is Attestation.UNCHECKED


def test_pack_with_a_key_refuses_sidecars_another_key_signed(tmp_path: Path) -> None:
    _run(tmp_path / "runs", signer=Signer.generate())
    key, _ = keygen(tmp_path / "keys")
    with pytest.raises(PackError, match="signature does not verify"):
        build(tmp_path / "runs", tmp_path / "p.zip", signer=Signer.from_file(key))
    assert not (tmp_path / "p.zip").exists()


# -- LC-2: with a key, unsigned and unattested fail -------------------------------------


def test_unsigned_pack_fails_when_a_key_is_given(tmp_path: Path) -> None:
    key, pub = keygen(tmp_path / "keys")
    _run(tmp_path / "runs", signer=Signer.from_file(key))
    out = tmp_path / "u.zip"
    build(tmp_path / "runs", out)
    verdict = verify_pack(out, pub)
    assert verdict.status is PackStatus.UNSIGNED and verdict.unsigned and not verdict.ok
    assert verify_pack(out, None).ok  # no key: a warning, as before
    result = runner.invoke(app, ["verify-pack", str(out), "--pubkey", str(pub)])
    assert result.exit_code == 1 and "UNSIGNED" in result.output
    assert runner.invoke(app, ["verify-pack", str(out)]).exit_code == 0


def test_run_without_its_sidecar_fails_a_keyed_pack(tmp_path: Path) -> None:
    key, pub = keygen(tmp_path / "keys")
    _run(tmp_path / "runs", "signed", signer=Signer.from_file(key))
    _run(tmp_path / "runs", "bare")
    out = tmp_path / "p.zip"
    build(tmp_path / "runs", out, signer=Signer.from_file(key))
    verdict = verify_pack(out, pub)
    assert verdict.status is PackStatus.ATTESTED
    assert verdict.unattested == ["bare"] and not verdict.ok
    result = runner.invoke(app, ["verify-pack", str(out), "--pubkey", str(pub)])
    assert result.exit_code == 1 and "UNATTESTED" in result.output and "bare" in result.output
    assert runner.invoke(app, ["verify-pack", str(out)]).exit_code == 0


# -- LC-4: verifying a pack is bounded ------------------------------------------------


def _plain_pack(tmp_path: Path) -> Path:
    _run(tmp_path / "runs")
    out = tmp_path / "p.zip"
    build(tmp_path / "runs", out)
    return out


@pytest.mark.parametrize(
    ("limit", "value", "reason"),
    [
        ("_MAX_MANIFEST", 100, "pack.json is larger than"),
        ("_MAX_TOTAL", 100, "members add up to more than"),
        ("_MAX_MEMBER", 100, "runs/r.jsonl: larger than"),
    ],
)
def test_oversized_packs_are_forged_before_inflating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, value: int, reason: str
) -> None:
    out = _plain_pack(tmp_path)
    monkeypatch.setattr(pack, limit, value)
    verdict = verify_pack(out, None)
    assert verdict.status is PackStatus.FORGED and reason in (verdict.reason or ""), verdict.reason


def test_highly_compressed_member_is_forged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _plain_pack(tmp_path)
    monkeypatch.setattr(pack, "_RATIO_FLOOR", 0)
    monkeypatch.setattr(pack, "_MAX_RATIO", 1)
    verdict = verify_pack(out, None)
    assert verdict.status is PackStatus.FORGED and "compressed more than" in (verdict.reason or "")


def test_members_are_streamed_once_never_read_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _plain_pack(tmp_path)

    def whole(*_: object, **__: object) -> bytes:
        raise AssertionError("read whole")

    monkeypatch.setattr(zipfile.ZipFile, "read", whole)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", whole)
    monkeypatch.setattr(pack, "_CHUNK", 7)
    verdict = verify_pack(out, None)
    assert verdict.ok and verdict.ledgers[0].chain == "ok", verdict.reason


# -- LC-10: hostile zips are FORGED, not tracebacks ----------------------------------


def _patch_entry(data: bytes, name: str, offset: int, value: int) -> bytes:
    """Set a 2-byte field of `name`'s local header (`offset` as there) and its central record."""
    raw = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        local = zf.getinfo(name).header_offset
    central = raw.index(b"PK\x01\x02")
    while bytes(raw[central + 46 : central + 46 + len(name)]) != name.encode():
        central = raw.index(b"PK\x01\x02", central + 4)
    raw[central + offset + 2 : central + offset + 4] = value.to_bytes(2, "little")
    raw[local + offset : local + offset + 2] = value.to_bytes(2, "little")
    return bytes(raw)


@pytest.mark.parametrize(
    ("name", "offset", "value", "reason"),
    [
        ("runs/r.jsonl", 6, 0x1, "runs/r.jsonl: encrypted"),
        ("runs/r.jsonl", 8, 99, "runs/r.jsonl: compression method 99"),
        (MANIFEST, 8, 99, "pack.json: compression method 99"),
    ],
)
def test_encrypted_or_exotic_members_are_forged(
    tmp_path: Path, name: str, offset: int, value: int, reason: str
) -> None:
    out = _plain_pack(tmp_path)
    out.write_bytes(_patch_entry(out.read_bytes(), name, offset, value))
    verdict = verify_pack(out, None)
    assert verdict.status is PackStatus.FORGED and reason in (verdict.reason or ""), verdict.reason


def test_bzip2_member_is_forged(tmp_path: Path) -> None:
    out = _plain_pack(tmp_path)
    members = _members(out)
    bz = tmp_path / "bz.zip"
    with zipfile.ZipFile(bz, "w", zipfile.ZIP_BZIP2) as zf:
        for n, d in members.items():
            zf.writestr(n, d)
    verdict = verify_pack(bz, None)
    assert verdict.status is PackStatus.FORGED and "compression method 12" in (verdict.reason or "")


def test_corrupt_manifest_is_forged_not_a_traceback(tmp_path: Path) -> None:
    out = _plain_pack(tmp_path)
    with zipfile.ZipFile(out) as zf:
        info = zf.getinfo(MANIFEST)
    raw = bytearray(out.read_bytes())
    raw[info.header_offset + 30 + len(info.filename) + len(info.extra) + 5] ^= 0xFF
    out.write_bytes(bytes(raw))
    result = runner.invoke(app, ["verify-pack", str(out)])
    assert result.exit_code == 1 and "FORGED" in result.output and MANIFEST in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# -- LC-6: run ids and scenario ids stay inside the pack ------------------------------


def _unsigned_raw(tmp_path: Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    out = _plain_pack(tmp_path)
    members = _members(out)
    return members, json.loads(members[MANIFEST])


def test_run_id_outside_the_pack_is_forged(tmp_path: Path) -> None:
    elsewhere = _run(tmp_path / "elsewhere", "genuine")
    members, manifest = _unsigned_raw(tmp_path)
    manifest["runs"].append(
        {"run_id": str(elsewhere.with_suffix("")), "events": 3, "complete": True, "attested": False}
    )
    _reseal(members, manifest)
    verdict = verify_pack(_zip(members, tmp_path / "abs.zip"), None)
    assert verdict.status is PackStatus.FORGED and "not a pack manifest" in (verdict.reason or "")
    with pytest.raises(ValueError, match="run_id"):
        PackManifest.model_validate({**manifest, "runs": [{**manifest["runs"][0], "run_id": ".."}]})


def test_run_listed_twice_or_orphan_sidecar_is_forged(tmp_path: Path) -> None:
    members, manifest = _unsigned_raw(tmp_path)
    twice = {**manifest, "runs": manifest["runs"] * 2}
    _reseal(members, twice)
    verdict = verify_pack(_zip(members, tmp_path / "twice.zip"), None)
    assert verdict.status is PackStatus.FORGED and "listed twice" in (verdict.reason or "")
    members, manifest = _unsigned_raw(tmp_path / "b")
    members["runs/ghost.attest.json"] = b"{}"
    _reseal(members, manifest)
    verdict = verify_pack(_zip(members, tmp_path / "orphan.zip"), None)
    assert verdict.status is PackStatus.FORGED
    assert "runs/ghost.attest.json is not listed" in (verdict.reason or "")


def test_finding_scenario_id_outside_the_pack_is_forged(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run(ROOT / "scenarios", target, runs)
    out = tmp_path / "s.zip"
    build(runs, out)
    members = _members(out)
    report = json.loads(members["findings.json"])
    finding = report["findings"][0]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.jsonl").write_bytes(members[f"runs/{finding['scenario_id']}.jsonl"])
    finding["scenario_id"] = str(outside / "x")
    members["findings.json"] = json.dumps(report).encode()
    manifest = json.loads(members[MANIFEST])
    _reseal(members, manifest)
    verdict = verify_pack(_zip(members, tmp_path / "f.zip"), None)
    assert verdict.status is PackStatus.FORGED and "not a run id" in (verdict.reason or "")


def test_pack_refuses_a_ledger_whose_name_is_not_a_run_id(tmp_path: Path) -> None:
    ledger = _run(tmp_path / "runs")
    ledger.rename(ledger.with_name("a b.jsonl"))
    with pytest.raises(PackError, match="run id"):
        build(tmp_path / "runs", tmp_path / "p.zip")


def test_recorder_refuses_dot_only_run_ids(tmp_path: Path) -> None:
    for run_id in (".", "..", "..."):
        with (
            pytest.raises(ValueError, match="run_id"),
            Recorder.start(tmp_path, "a", run_id=run_id),
        ):
            pass


# -- IER-3: pack and ledger text reaches the terminal escaped -------------------------


def test_verify_pack_escapes_member_names(tmp_path: Path) -> None:
    members, manifest = _unsigned_raw(tmp_path)
    members["runs/\x1b[2K\x1b[1Aok.jsonl"] = b"x"
    _reseal(members, manifest)  # listed as a member, so the reason quotes the raw name
    result = runner.invoke(app, ["verify-pack", str(_zip(members, tmp_path / "e.zip"))])
    assert result.exit_code == 1 and "FORGED" in result.output
    assert "\x1b" not in result.output and "\\x1b[2K" in result.output


def test_verify_attest_and_pack_escape_ledger_paths_and_reasons(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    bad = runs / "\x1b]52;c;aGk=\x07.jsonl"
    bad.write_text("{nope\n")
    for args in (
        ["verify", str(bad)],
        ["attest", str(bad), "--key", str(tmp_path / "none.key")],
        ["pack", str(runs), "--out", str(tmp_path / "p.zip")],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 1, args
        assert "\x1b" not in result.output and "\x07" not in result.output, args
        assert "\\x1b]52" in result.output, (args, result.output)
    sidecar(bad).write_text("{}")
    result = runner.invoke(app, ["attest", str(bad), "--key", str(tmp_path / "none.key")])
    assert result.exit_code == 1 and "\x1b" not in result.output and "exists" in result.output
