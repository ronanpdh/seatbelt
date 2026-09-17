import base64
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seatbelt.attest.manifest import AttestError, Manifest, build, sidecar
from seatbelt.attest.sign import KEY_FILE, PUB_FILE, Signer, attest, keygen, load_public_key
from seatbelt.cli import app
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
from seatbelt.record.recorder import Recorder
from seatbelt.verify.attest import Attestation, verify_attestation
from seatbelt.verify.chain import verify_file

runner = CliRunner()


def _ledger(tmp_path: Path) -> Path:
    with Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec:
        rec.outcome("done", success=True)
    return tmp_path / "r.jsonl"


def test_build_describes_the_finished_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    events = list(read_events(ledger))
    m = build(ledger)
    assert (m.run_id, m.events, m.final_hash) == ("r", 3, events[-1].hash)
    assert m.schema_version == events[-1].schema_version
    assert len(m.ledger_sha256) == 64
    assert m.signature == "" and m.public_key == ""


def test_sidecar_path_sits_beside_the_ledger() -> None:
    assert sidecar(Path("runs/abc.jsonl")) == Path("runs/abc.attest.json")


def test_canonical_excludes_signature_only() -> None:
    a = build_stub()
    b = a.model_copy(update={"signature": "x"})
    assert a.canonical() == b.canonical()
    assert a.model_copy(update={"events": 99}).canonical() != a.canonical()


def build_stub() -> Manifest:
    return Manifest(
        attest_version=1,
        run_id="r",
        schema_version=1,
        events=3,
        final_hash="f" * 64,
        ledger_sha256="d" * 64,
        harness="seatbelt test",
    )


def test_keygen_writes_private_key_0600_and_refuses_overwrite(tmp_path: Path) -> None:
    key, pub = keygen(tmp_path / "keys")
    assert key.name == KEY_FILE and pub.name == PUB_FILE
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert b"PRIVATE KEY" in key.read_bytes() and b"PUBLIC KEY" in pub.read_bytes()
    with pytest.raises(AttestError, match="refusing to overwrite"):
        keygen(tmp_path / "keys")


def test_signature_verifies_with_the_public_key_file(tmp_path: Path) -> None:
    key, pub = keygen(tmp_path)
    signed = Signer.from_file(key).sign(build_stub())
    load_public_key(pub).verify(base64.b64decode(signed.signature), signed.canonical())
    assert base64.b64decode(signed.public_key) == load_public_key(pub).public_bytes_raw()


def test_from_file_rejects_a_non_ed25519_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "rsa.key"
    pem.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(AttestError, match="Ed25519"):
        Signer.from_file(pem)
    (tmp_path / "junk.key").write_text("not pem")
    with pytest.raises(AttestError, match="PEM"):
        Signer.from_file(tmp_path / "junk.key")


def test_attest_writes_a_private_sidecar_and_refuses_a_second(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    key, _ = keygen(tmp_path / "keys")
    out = attest(ledger, Signer.from_file(key))
    assert out == sidecar(ledger)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    m = Manifest.model_validate_json(out.read_bytes())
    assert m.events == 3 and m.signature
    with pytest.raises(AttestError, match="exists"):
        attest(ledger, Signer.from_file(key))


def _attested(tmp_path: Path) -> tuple[Path, Path]:
    ledger = _ledger(tmp_path)
    key, pub = keygen(tmp_path / "keys")
    attest(ledger, Signer.from_file(key))
    return ledger, pub


def test_untouched_ledger_is_attested(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    assert verify_attestation(ledger, pub).status is Attestation.ATTESTED


def test_no_sidecar_is_unattested_and_no_pubkey_is_unchecked(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert verify_attestation(ledger, None).status is Attestation.UNATTESTED
    attest(ledger, Signer.generate())
    assert verify_attestation(ledger, None).status is Attestation.UNCHECKED


def test_forged_tail_passes_the_chain_but_not_attestation(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-2]) + "\n")  # drop outcome and run.end
    Ledger(ledger, "r").append(
        Kind.RUN_END,
        Actor(type=ActorType.AGENT, id="bot"),
        {"run.ok": True, "run.error": None, "run.events": 2},
    )
    chain = verify_file(ledger)
    assert chain.ok and chain.complete  # the ADR 0001 gap
    verdict = verify_attestation(ledger, pub)
    assert verdict.status is Attestation.FORGED
    assert "events" in (verdict.reason or "")


def test_any_byte_change_is_forged(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    with ledger.open("a") as fh:
        fh.write("\n")  # blank line: chain still verifies, digest does not
    assert verify_file(ledger).ok
    assert verify_attestation(ledger, pub).status is Attestation.FORGED


def test_truncated_ledger_is_forged(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-1]) + "\n")
    assert verify_attestation(ledger, pub).status is Attestation.FORGED


def test_wrong_key_edited_or_garbage_sidecar_is_forged(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    _, other = keygen(tmp_path / "other")
    assert verify_attestation(ledger, other).status is Attestation.FORGED
    side = sidecar(ledger)
    side.write_text(side.read_text().replace('"events":3', '"events":2'))
    assert verify_attestation(ledger, pub).status is Attestation.FORGED
    side.write_text("{not json")
    assert verify_attestation(ledger, pub).status is Attestation.FORGED


def test_bad_pubkey_file_raises(tmp_path: Path) -> None:
    ledger, _ = _attested(tmp_path)
    (tmp_path / "bad.pub").write_text("nope")
    with pytest.raises(AttestError, match="PEM"):
        verify_attestation(ledger, tmp_path / "bad.pub")


def test_non_utf8_byte_in_ledger_is_forged(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    with ledger.open("ab") as fh:
        fh.write(b"\xff\n")
    assert verify_attestation(ledger, pub).status is Attestation.FORGED


def test_unsupported_attest_version_is_forged(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    key, pub = keygen(tmp_path / "keys")
    manifest = build(ledger).model_copy(update={"attest_version": 99})
    sidecar(ledger).write_text(Signer.from_file(key).sign(manifest).model_dump_json())
    verdict = verify_attestation(ledger, pub)
    assert verdict.status is Attestation.FORGED
    assert "attest_version" in (verdict.reason or "")


def test_missing_pubkey_file_raises(tmp_path: Path) -> None:
    ledger, _ = _attested(tmp_path)
    with pytest.raises(AttestError):
        verify_attestation(ledger, tmp_path / "missing.pub")


def test_recorder_signs_at_run_end_even_when_the_run_fails(tmp_path: Path) -> None:
    key, pub = keygen(tmp_path / "keys")
    signer = Signer.from_file(key)
    with Recorder.start(tmp_path, agent_id="bot", run_id="ok", signer=signer) as rec:
        rec.outcome("done", success=True)
    assert verify_attestation(tmp_path / "ok.jsonl", pub).status is Attestation.ATTESTED
    with (
        pytest.raises(RuntimeError),
        Recorder.start(tmp_path, agent_id="bot", run_id="bad", signer=signer),
    ):
        raise RuntimeError("boom")
    assert verify_attestation(tmp_path / "bad.jsonl", pub).status is Attestation.ATTESTED


def test_cli_keygen_attest_verify_round_trip(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    assert runner.invoke(app, ["keygen", str(keys)]).exit_code == 0
    assert runner.invoke(app, ["keygen", str(keys)]).exit_code == 1
    ledger = _ledger(tmp_path)
    key, pub = keys / KEY_FILE, keys / PUB_FILE
    plain = runner.invoke(app, ["verify", str(ledger)])
    assert plain.exit_code == 0 and "unattested" in plain.output
    signed = runner.invoke(app, ["attest", str(ledger), "--key", str(key)])
    assert signed.exit_code == 0, signed.output
    again = runner.invoke(app, ["attest", str(ledger), "--key", str(key)])
    assert again.exit_code == 1 and "UNCHECKED" not in again.output
    unchecked = runner.invoke(app, ["verify", str(ledger)])
    assert unchecked.exit_code == 0 and "UNCHECKED" in unchecked.output
    good = runner.invoke(app, ["verify", str(ledger), "--pubkey", str(pub)])
    assert good.exit_code == 0 and "attested" in good.output
    assert runner.invoke(app, ["reconstruct", str(ledger), "--pubkey", str(pub)]).exit_code == 0


def test_cli_verify_reports_forged_and_attest_refuses_broken(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    with ledger.open("a") as fh:
        fh.write("\n")
    forged = runner.invoke(app, ["verify", str(ledger), "--pubkey", str(pub)])
    assert forged.exit_code == 1 and "FORGED" in forged.output
    assert runner.invoke(app, ["reconstruct", str(ledger), "--pubkey", str(pub)]).exit_code == 1
    ledger.write_text(ledger.read_text().replace("done", "undone"))
    sidecar(ledger).unlink()
    broken = runner.invoke(app, ["attest", str(ledger), "--key", str(tmp_path / "keys" / KEY_FILE)])
    assert broken.exit_code == 1 and "BROKEN" in broken.output
    assert not sidecar(ledger).exists()


def test_recorder_refuses_a_stale_sidecar_when_signing(tmp_path: Path) -> None:
    (tmp_path / "r.attest.json").write_text("{}")
    with (
        pytest.raises(FileExistsError),
        Recorder.start(tmp_path, agent_id="bot", run_id="r", signer=Signer.generate()),
    ):
        pass
    assert not (tmp_path / "r.jsonl").exists()
    with Recorder.start(tmp_path, agent_id="bot", run_id="r"):
        pass  # no signer: the stale sidecar is not our business


def test_cli_attest_refuses_incomplete_ledger_and_missing_key(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    key, _ = keygen(tmp_path / "keys")
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-1]) + "\n")
    short = runner.invoke(app, ["attest", str(ledger), "--key", str(key)])
    assert short.exit_code == 1 and "INCOMPLETE" in short.output
    assert not sidecar(ledger).exists()
    ledger.write_text("\n".join(lines) + "\n")
    missing = runner.invoke(app, ["attest", str(ledger), "--key", str(tmp_path / "nope.key")])
    assert missing.exit_code == 1 and "not a PEM private key" in missing.output
    assert not sidecar(ledger).exists()


def test_keygen_refuses_a_stale_pub_without_leaving_a_key(tmp_path: Path) -> None:
    (tmp_path / PUB_FILE).write_text("stale")
    with pytest.raises(AttestError, match="refusing to overwrite"):
        keygen(tmp_path)
    assert not (tmp_path / KEY_FILE).exists()


def test_pubkey_without_sidecar_loads_the_key_and_the_cli_exits_one(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _, pub = keygen(tmp_path / "keys")
    assert verify_attestation(ledger, pub).status is Attestation.UNATTESTED
    (tmp_path / "typo.pub").write_text("nope")
    with pytest.raises(AttestError, match="PEM"):
        verify_attestation(ledger, tmp_path / "typo.pub")
    missing = runner.invoke(app, ["verify", str(ledger), "--pubkey", str(pub)])
    assert missing.exit_code == 1 and "UNATTESTED" in missing.output
    assert runner.invoke(app, ["verify", str(ledger)]).exit_code == 0
