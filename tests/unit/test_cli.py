from pathlib import Path

from typer.testing import CliRunner

from seatbelt import __version__
from seatbelt.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_demo_then_verify_then_reconstruct(tmp_path: Path) -> None:
    demo = runner.invoke(app, ["demo", "--out", str(tmp_path)])
    assert demo.exit_code == 0, demo.output
    ledger = next(tmp_path.glob("*.jsonl"))
    assert runner.invoke(app, ["verify", str(ledger)]).exit_code == 0
    shown = runner.invoke(app, ["reconstruct", str(ledger)])
    assert shown.exit_code == 0
    assert "decision" in shown.output


def test_verify_fails_on_tampered_ledger(tmp_path: Path) -> None:
    runner.invoke(app, ["demo", "--out", str(tmp_path)])
    ledger = next(tmp_path.glob("*.jsonl"))
    ledger.write_text(ledger.read_text().replace("refund issued", "refund denied"))
    result = runner.invoke(app, ["verify", str(ledger)])
    assert result.exit_code == 1
    assert "BROKEN" in result.output


def test_verify_fails_cleanly_on_corrupt_or_missing_ledger(tmp_path: Path) -> None:
    corrupt = tmp_path / "r.jsonl"
    corrupt.write_text("not json\n")
    for path in (corrupt, tmp_path / "missing.jsonl"):
        result = runner.invoke(app, ["verify", str(path)])
        assert result.exit_code == 1
        assert "BROKEN" in result.output
