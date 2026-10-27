"""Every command that checks a ledger and then signs, shows, counts or packs it uses the bytes
it checked: one read, never a second one that could be of a file changed in between."""

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

import pytest
from typer.testing import CliRunner

from seatbelt.attest.manifest import Manifest, sidecar
from seatbelt.attest.sign import Signer, keygen
from seatbelt.cli import _run_row, app  # pyright: ignore[reportPrivateUsage]
from seatbelt.ledger.events import GENESIS_HASH
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder
from seatbelt.report.fleet import fleet
from seatbelt.report.html import build as build_page
from seatbelt.report.pack import build as build_pack

runner = CliRunner()
GENUINE, TAMPERED = "Refund order 1001", "Wire $50,000 to acct 999"


def _run(tmp: Path, signed: bool = True) -> tuple[Path, Path, Path]:
    """A finished run's ledger, signed unless not `signed`, and the key pair."""
    key, pub = keygen(tmp / "keys")
    signer = Signer.from_file(key) if signed else None
    with Recorder.start(tmp / "runs", agent_id="bot", run_id="run", signer=signer) as rec:
        rec.user_message("u", GENUINE)
        rec.outcome("refund issued", success=True)
    return tmp / "runs" / "run.jsonl", key, pub


def _rechained(ledger: Path) -> bytes:
    """`ledger` with the user's words changed and its chain recomputed, which needs no key."""
    prev, lines = GENESIS_HASH, list[str]()
    for event in read_events(ledger):
        attrs = json.loads(json.dumps(event.attrs).replace(GENUINE, TAMPERED))
        event = event.model_copy(update={"attrs": attrs, "prev_hash": prev}).sealed()
        prev = event.hash
        lines.append(event.model_dump_json())
    return ("\n".join(lines) + "\n").encode()


def _changes_after_first_read(monkeypatch: pytest.MonkeyPatch, ledger: Path, later: bytes) -> None:
    """Every read of `ledger` after the first sees `later`: the file was written to after the
    command first read it."""
    other = ledger.with_name(ledger.name + ".later")
    other.write_bytes(later)
    real = cast(Callable[..., IO[Any]], Path.open)
    opened = 0

    def open_(self: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        nonlocal opened
        if self == ledger:
            opened += 1
            if opened > 1:
                return real(other, *args, **kwargs)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)


def _closes_on_first_read(
    monkeypatch: pytest.MonkeyPatch, ledger: Path, close: Callable[[], None]
) -> None:
    """`close` runs just after the first read of `ledger`, as a run that ends meanwhile."""
    real = cast(Callable[..., IO[Any]], Path.open)
    done = False

    def open_(self: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        nonlocal done
        handle = real(self, *args, **kwargs)
        if self != ledger or done:
            return handle
        done = True
        with handle:
            data: bytes | str = handle.read()
        close()
        return io.BytesIO(data) if isinstance(data, bytes) else io.StringIO(data)

    monkeypatch.setattr(Path, "open", open_)


def _open_run(tmp: Path) -> tuple[Path, Callable[[], None]]:
    """A run's ledger as it is before its run.end and signature, and what writes them."""
    ledger, _, _ = _run(tmp)
    closed, signature = ledger.read_bytes(), sidecar(ledger).read_bytes()
    ledger.write_bytes(closed[: closed.rstrip(b"\n").rindex(b"\n") + 1])
    sidecar(ledger).unlink()

    def close() -> None:
        ledger.write_bytes(closed)
        sidecar(ledger).write_bytes(signature)

    return ledger, close


def test_attest_signs_the_bytes_it_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, key, pub = _run(tmp_path, signed=False)
    genuine, tampered = ledger.read_bytes(), _rechained(ledger)
    _changes_after_first_read(monkeypatch, ledger, tampered)
    result = runner.invoke(app, ["attest", str(ledger), "--key", str(key)])
    monkeypatch.undo()
    assert result.exit_code == 0, result.output
    signed = Manifest.model_validate_json(sidecar(ledger).read_bytes())
    assert signed.ledger_sha256 == hashlib.sha256(genuine).hexdigest()
    ledger.write_bytes(tampered)  # so the edit, re-chained, is not passed off as signed
    forged = runner.invoke(app, ["verify", str(ledger), "--pubkey", str(pub)])
    assert forged.exit_code == 1
    assert "FORGED" in forged.output


def test_reconstruct_shows_the_events_it_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _, pub = _run(tmp_path)
    _changes_after_first_read(monkeypatch, ledger, _rechained(ledger))
    result = runner.invoke(app, ["reconstruct", str(ledger), "--pubkey", str(pub)])
    assert result.exit_code == 0, result.output
    assert "Refund" in result.output
    assert "Wire" not in result.output


def test_verify_checks_the_signature_against_the_bytes_it_chained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edited and re-chained, then the signed bytes put back for the reads that check the
    signature: one read sees the edit, and the signature does not match it."""
    ledger, _, pub = _run(tmp_path)
    genuine = ledger.read_bytes()
    ledger.write_bytes(_rechained(ledger))
    _changes_after_first_read(monkeypatch, ledger, genuine)
    result = runner.invoke(app, ["verify", str(ledger), "--pubkey", str(pub)])
    assert result.exit_code == 1
    assert "FORGED" in result.output
    monkeypatch.undo()
    _changes_after_first_read(monkeypatch, ledger, genuine)
    as_json = runner.invoke(app, ["verify", str(ledger), "--pubkey", str(pub), "--json"])
    assert json.loads(as_json.output)["signature"] == "forged"


def test_report_and_runs_count_and_check_the_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _, pub = _run(tmp_path)
    genuine = ledger.read_bytes()
    ledger.write_bytes(_rechained(ledger))
    _changes_after_first_read(monkeypatch, ledger, genuine)
    report = fleet(ledger.parent, pub)
    assert report.forged == ["run"]
    assert report.runs == 0
    monkeypatch.undo()
    _changes_after_first_read(monkeypatch, ledger, genuine)
    assert _run_row(ledger, pub)["signature"] == "forged"


def test_pack_carries_the_bytes_it_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, _, _ = _run(tmp_path, signed=False)
    genuine = ledger.read_bytes()
    _changes_after_first_read(monkeypatch, ledger, b"not a ledger\n")
    build_pack(ledger.parent, tmp_path / "audit.zip")
    monkeypatch.undo()
    with zipfile.ZipFile(tmp_path / "audit.zip") as zf:
        assert zf.read("runs/run.jsonl") == genuine


def test_a_run_signed_while_it_is_checked_is_not_forged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signature file is read before the ledger: a ledger is not written to once signed,
    so a run that closes and is signed between the two reads is seen open and unsigned."""
    ledger, close = _open_run(tmp_path)
    _closes_on_first_read(monkeypatch, ledger, close)
    result = runner.invoke(app, ["verify", str(ledger)])
    assert result.exit_code == 1  # incomplete, as it was when read
    assert "INCOMPLETE" in result.output
    assert "FORGED" not in result.output


def test_a_run_signed_while_its_page_is_made_gets_the_page_it_was_read_as(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, close = _open_run(tmp_path)
    _closes_on_first_read(monkeypatch, ledger, close)
    page = build_page(ledger)
    assert page.outcome == "incomplete"
    assert page.signature == "unattested"
