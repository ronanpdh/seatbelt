from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from seatbelt import __version__
from seatbelt.cli import app
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger
from seatbelt.record.recorder import Recorder
from seatbelt.report.timeline import timeline

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


def test_verify_flags_truncated_ledger(tmp_path: Path) -> None:
    runner.invoke(app, ["demo", "--out", str(tmp_path)])
    ledger = next(tmp_path.glob("*.jsonl"))
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-2]) + "\n")
    result = runner.invoke(app, ["verify", str(ledger)])
    assert result.exit_code == 1
    assert "INCOMPLETE" in result.output


def test_reconstruct_shows_ledger_text_literally(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec:
        rec.decision("refund " + "y" * 120, authority="agent:auto", basis=[])
        rec.outcome("line one\n" + "[black on black]hidden[/] " + "x" * 200, success=True)
    console = Console(width=300, record=True)
    timeline(tmp_path / "r.jsonl", console)
    out = console.export_text()
    assert "[authority: agent:auto]" in out
    assert "[black on black]hidden[/]" in out
    assert "x" * 100 not in out


def test_verify_fails_cleanly_on_corrupt_or_missing_ledger(tmp_path: Path) -> None:
    corrupt = tmp_path / "r.jsonl"
    corrupt.write_text("not json\n")
    for path in (corrupt, tmp_path / "missing.jsonl"):
        result = runner.invoke(app, ["verify", str(path)])
        assert result.exit_code == 1
        assert "BROKEN" in result.output


def test_reconstruct_refuses_a_tampered_ledger(tmp_path: Path) -> None:
    runner.invoke(app, ["demo", "--out", str(tmp_path)])
    ledger = next(tmp_path.glob("*.jsonl"))
    ledger.write_text(ledger.read_text().replace("refund issued", "refund denied"))
    result = runner.invoke(app, ["reconstruct", str(ledger)])
    assert result.exit_code == 1
    assert "BROKEN" in result.output
    assert "refund denied" not in result.output


def test_reconstruct_fails_cleanly_on_corrupt_or_missing_ledger(tmp_path: Path) -> None:
    corrupt = tmp_path / "r.jsonl"
    corrupt.write_text("not json\n")
    binary = tmp_path / "b.jsonl"
    binary.write_bytes(b"\xff\xfe{}\n")
    for path in (corrupt, binary, tmp_path / "missing.jsonl"):
        result = runner.invoke(app, ["reconstruct", str(path)])
        assert result.exit_code == 1
        assert "BROKEN" in result.output
        assert "Traceback" not in result.output


def test_reconstruct_shows_run_start_and_end(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError),
        Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec,
        rec.model_call("m", {}),
    ):
        raise RuntimeError("boom")
    console = Console(width=300, record=True)
    timeline(tmp_path / "r.jsonl", console)
    out = console.export_text()
    assert f"seatbelt {__version__}" in out
    assert "RuntimeError: boom" in out.split("model.response")[1].splitlines()[0]
    assert "FAILED: RuntimeError: boom" in out


def test_reconstruct_shows_a_failed_run_without_a_recorded_reason(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "old.jsonl", "old")
    ledger.append(Kind.RUN_START, Actor(type=ActorType.SYSTEM, id="seatbelt"))
    ledger.append(Kind.RUN_END, Actor(type=ActorType.AGENT, id="bot"), {"run.ok": False})
    console = Console(width=300, record=True)
    timeline(tmp_path / "old.jsonl", console)
    line = next(ln for ln in console.export_text().splitlines() if "run.end" in ln)
    assert "FAILED" in line
    assert "None" not in line


GATEWAY_YAML = (
    "signing_key: k\nledgers: runs\nupstreams:\n  anthropic: {url: https://x, key_env: K}\n"
)


def test_gateway_keygen_appends_a_principal(tmp_path: Path) -> None:
    cfg = tmp_path / "gateway.yaml"
    cfg.write_text(GATEWAY_YAML)
    r = runner.invoke(app, ["gateway", "keygen", "--user", "alice@corp", "--config", str(cfg)])
    assert r.exit_code == 0 and "sbk_" in r.output and "alice@corp" in cfg.read_text()
    key = next(w for w in r.output.split() if w.startswith("sbk_"))
    assert key not in cfg.read_text()
    r = runner.invoke(app, ["gateway", "keygen", "--user", "alice@corp", "--config", str(cfg)])
    assert r.exit_code == 1 and "already" in r.output


def test_gateway_serve_refuses_bad_config(tmp_path: Path) -> None:
    r = runner.invoke(app, ["gateway", "serve", "--config", str(tmp_path / "missing.yaml")])
    assert r.exit_code == 1 and "missing.yaml" in r.output


def test_gateway_serve_refuses_a_missing_signing_key(tmp_path: Path) -> None:
    cfg = tmp_path / "gateway.yaml"
    cfg.write_text(GATEWAY_YAML)
    r = runner.invoke(app, ["gateway", "serve", "--config", str(cfg)])
    assert r.exit_code == 1 and "not a PEM private key" in r.output


def test_gateway_serve_recovers_then_serves_then_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from seatbelt.attest.manifest import sidecar
    from seatbelt.attest.sign import keygen
    from seatbelt.gateway import serve as serve_mod
    from seatbelt.gateway.config import add_principal, load_config
    from seatbelt.verify.chain import verify_file

    keygen(tmp_path / "keys")
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text(
        GATEWAY_YAML.replace("signing_key: k", "signing_key: keys/seatbelt.key")
        + "listen: '[::1]:9999'\n"
    )
    key = add_principal(cfg_path, "alice@corp")
    cfg = load_config(cfg_path)
    crashed = Recorder.start(cfg.ledgers, agent_id="gateway", run_id="crashed")
    crashed.__enter__().user_message("u", "hi")  # never exited: a crash
    bound: list[tuple[str, int]] = []

    def fake_run(app: Starlette, host: str, port: int, **_: object) -> None:
        bound.append((host, port))
        app.state.transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json={}))
        with TestClient(app) as client:
            client.post("/v1/messages", json={"model": "m"}, headers={"x-api-key": key})

    monkeypatch.setattr(serve_mod.uvicorn, "run", fake_run)
    serve_mod.serve(cfg_path)
    assert bound == [("::1", 9999)]
    ledgers = sorted(cfg.ledgers.glob("*.jsonl"))
    assert len(ledgers) == 2  # the recovered crash and the session served
    assert all(verify_file(p).complete and sidecar(p).exists() for p in ledgers)
