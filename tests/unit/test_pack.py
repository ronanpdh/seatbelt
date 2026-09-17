import hashlib
import json
import warnings
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from examples.scenario_target import target
from typer.testing import CliRunner

from seatbelt.attest.manifest import AttestError
from seatbelt.attest.manifest import build as attest_build
from seatbelt.attest.sign import Signer, keygen
from seatbelt.cli import app
from seatbelt.record.recorder import Recorder
from seatbelt.report.pack import (
    MANIFEST,
    Member,
    PackError,
    PackManifest,
    PackStatus,
    build,
    verify_pack,
)
from seatbelt.scenarios.runner import run
from seatbelt.verify.attest import Attestation, verify_attestation

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()
_Edit = Callable[[dict[str, bytes]], object]


def _scenario_runs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A signed scenario run: (runs_dir, private key, public key)."""
    key, pub = keygen(tmp_path / "keys")
    runs = tmp_path / "runs"
    run(ROOT / "scenarios", target, runs, signer=Signer.from_file(key))
    return runs, key, pub


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_build_packs_ledgers_sidecars_findings_and_manifest_last(tmp_path: Path) -> None:
    runs, key, _ = _scenario_runs(tmp_path)
    out = tmp_path / "audit.seatbelt.zip"
    manifest = build(runs, out, signer=Signer.from_file(key), corpus=ROOT / "scenarios")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert names[-1] == MANIFEST and names[:-1] == sorted(names[:-1])
    assert "runs/benign-order-status.jsonl" in names
    assert "runs/benign-order-status.attest.json" in names
    assert "findings.json" in names and "corpus/benign-order-status.yaml" in names
    assert {m.path for m in manifest.members} == set(names) - {MANIFEST}
    assert manifest.findings == 1 and manifest.corpus_sha256
    assert all(r.complete and r.attested for r in manifest.runs) and len(manifest.runs) == 8
    assert manifest.signature and manifest.public_key
    members = _members(out)
    for m in manifest.members:
        assert hashlib.sha256(members[m.path]).hexdigest() == m.sha256
        assert len(members[m.path]) == m.bytes
    assert PackManifest.model_validate_json(members[MANIFEST]) == manifest


def test_build_is_deterministic_except_the_manifest(tmp_path: Path) -> None:
    runs, _, _ = _scenario_runs(tmp_path)
    build(runs, tmp_path / "a.zip")
    build(runs, tmp_path / "b.zip")
    a, b = _members(tmp_path / "a.zip"), _members(tmp_path / "b.zip")
    assert {k: v for k, v in a.items() if k != MANIFEST} == {
        k: v for k, v in b.items() if k != MANIFEST
    }
    with zipfile.ZipFile(tmp_path / "a.zip") as zf:
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())


def test_build_refuses_broken_ledger_existing_output_and_empty_dir(tmp_path: Path) -> None:
    runs, _, _ = _scenario_runs(tmp_path)
    with pytest.raises(PackError, match="no ledgers"):
        build(tmp_path / "empty", tmp_path / "x.zip")
    out = tmp_path / "audit.zip"
    build(runs, out)
    with pytest.raises(PackError, match="exists"):
        build(runs, out)
    with pytest.raises(PackError, match="missing"):
        build(runs, tmp_path / "missing" / "z.zip")
    ledger = runs / "benign-order-status.jsonl"
    ledger.write_text(ledger.read_text().replace("delivered", "lost"))
    with pytest.raises(PackError, match="BROKEN"):
        build(runs, tmp_path / "y.zip")
    assert not (tmp_path / "y.zip").exists()


def test_build_packs_incomplete_and_unattested_ledgers_honestly(tmp_path: Path) -> None:
    with Recorder.start(tmp_path / "runs", agent_id="bot", run_id="plain") as rec:
        rec.outcome("done", success=True)
    ledger = tmp_path / "runs" / "plain.jsonl"
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-1]) + "\n")
    manifest = build(tmp_path / "runs", tmp_path / "p.zip")
    assert manifest.runs[0].model_dump() == {
        "run_id": "plain",
        "events": 2,
        "complete": False,
        "attested": False,
    }
    assert manifest.findings is None and manifest.corpus_sha256 is None
    assert manifest.signature == ""
    members = _members(tmp_path / "p.zip")
    manifest.runs[0].complete = True
    _reseal(members, manifest)
    verdict = verify_pack(_zip(members, tmp_path / "hidden.zip"), None)
    assert verdict.status is PackStatus.FORGED and "run summary" in (verdict.reason or "")


def test_build_refuses_corpus_that_does_not_match_findings(tmp_path: Path) -> None:
    runs, _, _ = _scenario_runs(tmp_path)
    other = tmp_path / "corpus"
    other.mkdir()
    benign = (ROOT / "scenarios" / "benign-order-status.yaml").read_text()
    (other / "x.yaml").write_text(benign.replace("benign-order-status", "x"))
    with pytest.raises(PackError, match="does not match"):
        build(runs, tmp_path / "c.zip", corpus=other)
    with Recorder.start(tmp_path / "plain", agent_id="bot", run_id="r") as rec:
        rec.outcome("done", success=True)
    with pytest.raises(PackError, match=r"findings\.json"):
        build(tmp_path / "plain", tmp_path / "d.zip", corpus=ROOT / "scenarios")


def _zip(members: dict[str, bytes], dst: Path) -> Path:
    with zipfile.ZipFile(dst, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return dst


def _rezip(src: Path, dst: Path, edit: _Edit) -> Path:
    members = _members(src)
    edit(members)
    return _zip(members, dst)


def _reseal(members: dict[str, bytes], manifest: PackManifest, key: Path | None = None) -> None:
    """Recompute member hashes, re-sign with `key` (or leave unsigned) and store pack.json."""
    for m in manifest.members:
        m.sha256, m.bytes = hashlib.sha256(members[m.path]).hexdigest(), len(members[m.path])
    if key is not None:
        manifest = Signer.from_file(key).sign(manifest.model_copy(update={"signature": ""}))
    members[MANIFEST] = manifest.model_dump_json(indent=2).encode() + b"\n"


def _pack(tmp_path: Path) -> tuple[Path, Path]:
    runs, key, pub = _scenario_runs(tmp_path)
    out = tmp_path / "audit.seatbelt.zip"
    build(runs, out, signer=Signer.from_file(key), corpus=ROOT / "scenarios")
    return out, pub


def test_clean_pack_is_attested_and_every_ledger_ok(tmp_path: Path) -> None:
    out, pub = _pack(tmp_path)
    verdict = verify_pack(out, pub)
    assert verdict.status is PackStatus.ATTESTED and verdict.ok, verdict.reason
    assert len(verdict.ledgers) == 8
    assert all(s.chain == "ok" and s.attestation is Attestation.ATTESTED for s in verdict.ledgers)


def test_unsigned_and_unchecked_packs(tmp_path: Path) -> None:
    runs, _, pub = _scenario_runs(tmp_path)
    out = tmp_path / "u.zip"
    build(runs, out)
    assert verify_pack(out, pub).status is PackStatus.UNSIGNED
    signed, _ = _pack(tmp_path / "s")
    assert verify_pack(signed, None).status is PackStatus.UNCHECKED
    assert all(s.attestation is Attestation.UNCHECKED for s in verify_pack(signed, None).ledgers)


BENIGN = "runs/benign-order-status.jsonl"
BENIGN_SIDECAR = "runs/benign-order-status.attest.json"
TAMPERS: list[tuple[str, _Edit, str]] = [
    (
        "edit-ledger",
        lambda m: m.update({BENIGN: m[BENIGN] + b"\n"}),
        "benign-order-status.jsonl",
    ),
    (
        "edit-ledger-same-length",
        lambda m: m.update({BENIGN: m[BENIGN].replace(b"delivered", b"deliverod")}),
        "benign-order-status.jsonl",
    ),
    ("drop-member", lambda m: m.pop(BENIGN_SIDECAR), "benign-order-status.attest.json"),
    ("stray-member", lambda m: m.update({"runs/extra.txt": b"x"}), "runs/extra.txt"),
    (
        "edit-manifest",
        lambda m: m.update({MANIFEST: m[MANIFEST].replace(b'"findings": 1', b'"findings": 0')}),
        "signature",
    ),
    (
        "swap-sidecar",
        lambda m: m.update({BENIGN_SIDECAR: m["runs/poisoned-context.attest.json"]}),
        "benign-order-status.attest.json",
    ),
    ("no-manifest", lambda m: m.pop(MANIFEST), MANIFEST),
    ("garbage-manifest", lambda m: m.update({MANIFEST: b"{nope"}), "not a pack manifest"),
    ("zip-slip", lambda m: m.update({"../escape.txt": b"x"}), "unsafe"),
]


@pytest.mark.parametrize(("name", "edit", "reason"), TAMPERS)
def test_tampered_pack_is_forged_naming_the_member(
    tmp_path: Path, name: str, edit: _Edit, reason: str
) -> None:
    out, pub = _pack(tmp_path)
    bad = _rezip(out, tmp_path / f"{name}.zip", edit)
    verdict = verify_pack(bad, pub)
    assert verdict.status is PackStatus.FORGED and not verdict.ok
    assert reason in (verdict.reason or ""), verdict.reason
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path.parent / "escape.txt").exists()


def test_wrong_key_bad_key_and_not_a_zip(tmp_path: Path) -> None:
    out, _ = _pack(tmp_path)
    _, other = keygen(tmp_path / "other")
    assert verify_pack(out, other).status is PackStatus.FORGED
    (tmp_path / "bad.pub").write_text("nope")
    with pytest.raises(AttestError):
        verify_pack(out, tmp_path / "bad.pub")
    (tmp_path / "not.zip").write_text("hello")
    assert verify_pack(tmp_path / "not.zip", None).status is PackStatus.FORGED
    gone = verify_pack(tmp_path / "gone.zip", None)
    assert gone.status is PackStatus.FORGED and "No such file" in (gone.reason or "")
    assert "not a zip" not in (gone.reason or "")


def test_dangling_evidence_and_manifest_findings_mismatch_are_forged(tmp_path: Path) -> None:
    runs, key, pub = _scenario_runs(tmp_path)
    report_path = runs / "findings.json"
    report = json.loads(report_path.read_text())
    report["findings"][0]["evidence"][0] = "ffffffff" + report["findings"][0]["evidence"][0]
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    out = tmp_path / "dangling.zip"
    build(runs, out, signer=Signer.from_file(key))
    verdict = verify_pack(out, pub)
    assert verdict.status is PackStatus.FORGED and "evidence" in (verdict.reason or "")


def test_broken_ledger_inside_a_signed_pack_is_reported_not_hidden(tmp_path: Path) -> None:
    runs, key, pub = _scenario_runs(tmp_path)
    out = tmp_path / "ok.zip"
    build(runs, out, signer=Signer.from_file(key))
    # a tool that skipped the chain check: re-attest the broken ledger, re-sign the manifest
    signer = Signer.from_file(key)
    members = _members(out)
    manifest = PackManifest.model_validate_json(members[MANIFEST])
    scratch = tmp_path / "scratch" / "benign-order-status.jsonl"
    scratch.parent.mkdir()
    scratch.write_bytes(members[BENIGN].replace(b"delivered", b"lost"))
    members[BENIGN] = scratch.read_bytes()
    members[BENIGN_SIDECAR] = (signer.sign(attest_build(scratch)).model_dump_json() + "\n").encode()
    _reseal(members, manifest, key)
    verdict = verify_pack(_zip(members, tmp_path / "broken.zip"), pub)
    assert verdict.status is PackStatus.ATTESTED and not verdict.ok
    assert any(s.chain == "broken" and s.run_id == "benign-order-status" for s in verdict.ledgers)


def test_duplicate_entry_is_forged(tmp_path: Path) -> None:
    out, pub = _pack(tmp_path)
    members = _members(out)
    with zipfile.ZipFile(tmp_path / "dup.zip", "w") as zf, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate name we want
        for name, data in members.items():
            if name == BENIGN:
                zf.writestr(name, data.replace(b"delivered", b"lost"))
            zf.writestr(name, data)
    verdict = verify_pack(tmp_path / "dup.zip", pub)
    assert verdict.status is PackStatus.FORGED and "duplicate" in (verdict.reason or "")


def test_corrupt_compressed_member_is_forged(tmp_path: Path) -> None:
    out, pub = _pack(tmp_path)
    with zipfile.ZipFile(out) as zf:
        info = zf.getinfo(BENIGN)
    raw = bytearray(out.read_bytes())
    raw[info.header_offset + 30 + len(info.filename) + len(info.extra) + 5] ^= 0xFF
    out.write_bytes(bytes(raw))
    verdict = verify_pack(out, pub)
    assert verdict.status is PackStatus.FORGED and BENIGN in (verdict.reason or "")


def test_unreadable_ledger_named_by_findings_never_raises(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run(ROOT / "scenarios", target, runs)
    build(runs, tmp_path / "u.zip")
    members = _members(tmp_path / "u.zip")
    named = json.loads(members["findings.json"])["findings"][0]["scenario_id"]
    members[f"runs/{named}.jsonl"] += b"\xff\n"
    _reseal(members, PackManifest.model_validate_json(members[MANIFEST]))
    verdict = verify_pack(_zip(members, tmp_path / "bad.zip"), None)
    assert not verdict.ok
    assert verdict.status is PackStatus.FORGED or any(s.chain == "broken" for s in verdict.ledgers)


def test_member_outside_the_layout_is_forged(tmp_path: Path) -> None:
    runs, key, pub = _scenario_runs(tmp_path)
    build(runs, tmp_path / "ok.zip", signer=Signer.from_file(key))
    members = _members(tmp_path / "ok.zip")
    manifest = PackManifest.model_validate_json(members[MANIFEST])
    members["evil.jsonl"] = members[BENIGN]
    manifest.members.append(Member(path="evil.jsonl", sha256="", bytes=0))
    _reseal(members, manifest, key)
    verdict = verify_pack(_zip(members, tmp_path / "evil.zip"), pub)
    assert verdict.status is PackStatus.FORGED
    assert "evil.jsonl: not a pack member path" in (verdict.reason or "")


def test_corpus_without_a_hash_is_forged(tmp_path: Path) -> None:
    with Recorder.start(tmp_path / "runs", agent_id="bot", run_id="plain") as rec:
        rec.outcome("done", success=True)
    build(tmp_path / "runs", tmp_path / "p.zip")
    members = _members(tmp_path / "p.zip")
    manifest = PackManifest.model_validate_json(members[MANIFEST])
    members["corpus/x.yaml"] = b"id: x\n"
    manifest.members.append(Member(path="corpus/x.yaml", sha256="", bytes=0))
    _reseal(members, manifest)
    verdict = verify_pack(_zip(members, tmp_path / "c.zip"), None)
    assert verdict.status is PackStatus.FORGED and "corpus" in (verdict.reason or "")


def test_sidecar_in_the_0_0_4_field_order_still_verifies(tmp_path: Path) -> None:
    runs, _, pub = _scenario_runs(tmp_path)
    side = runs / "benign-order-status.attest.json"
    data = json.loads(side.read_text())
    tail = {k: data.pop(k) for k in ("public_key", "signature")}
    side.write_text(json.dumps({**data, **tail}) + "\n")
    assert next(iter(json.loads(side.read_text()))) == "attest_version"
    assert (
        verify_attestation(runs / "benign-order-status.jsonl", pub).status is Attestation.ATTESTED
    )


def test_cli_pack_and_verify_pack(tmp_path: Path) -> None:
    runs, key, pub = _scenario_runs(tmp_path)
    out = tmp_path / "audit.seatbelt.zip"
    made = runner.invoke(
        app,
        [
            "pack",
            str(runs),
            "--out",
            str(out),
            "--key",
            str(key),
            "--corpus",
            str(ROOT / "scenarios"),
        ],
    )
    assert made.exit_code == 0, made.output
    assert "wrote" in made.output
    assert runner.invoke(app, ["pack", str(runs), "--out", str(out)]).exit_code == 1
    good = runner.invoke(app, ["verify-pack", str(out), "--pubkey", str(pub)])
    assert good.exit_code == 0, good.output
    assert "attested" in good.output and "benign-order-status" in good.output
    unchecked = runner.invoke(app, ["verify-pack", str(out)])
    assert unchecked.exit_code == 0 and "UNCHECKED" in unchecked.output
    bad = _rezip(out, tmp_path / "bad.zip", lambda m: m.pop("findings.json"))
    forged = runner.invoke(app, ["verify-pack", str(bad), "--pubkey", str(pub)])
    assert forged.exit_code == 1 and "FORGED" in forged.output
    assert runner.invoke(app, ["verify-pack", str(tmp_path / "nope.zip")]).exit_code == 1


def test_pack_schema_is_exported() -> None:
    from seatbelt.report.pack import PackManifest as Model

    assert (
        json.loads((ROOT / "docs" / "schema" / "pack.json").read_text())
        == Model.model_json_schema()
    )
