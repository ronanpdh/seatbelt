import os
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from seatbelt import __version__
from seatbelt.attest.sign import Signer
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


def test_verify_json_says_what_the_exit_code_means_and_why(tmp_path: Path) -> None:
    """What a desktop app reads: the same checks as `verify`, as data."""
    import json

    runner.invoke(app, ["demo", "--out", str(tmp_path)])
    ledger = next(tmp_path.glob("*.jsonl"))
    text = ledger.read_text()
    r = runner.invoke(app, ["verify", str(ledger), "--json"])
    good = json.loads(r.output)
    assert r.exit_code == 0 and good["ok"] and good["chain"] == "intact" and good["complete"]
    assert good["ledger"] == str(ledger) and good["events"] > 0
    assert good["signature"] in ("attested", "unchecked", "unattested")
    lines = text.splitlines()
    ledger.write_text("\n".join(lines[:-2]) + "\n")
    r = runner.invoke(app, ["verify", str(ledger), "--json"])
    cut = json.loads(r.output)
    assert r.exit_code == 1 and not cut["ok"] and cut["chain"] == "intact"
    assert not cut["complete"] and "run.end" in cut["reason"]
    ledger.write_text(text.replace("refund issued", "refund denied"))
    r = runner.invoke(app, ["verify", str(ledger), "--json"])
    broken = json.loads(r.output)
    assert r.exit_code == 1 and broken["chain"] == "broken" and broken["signature"] is None
    assert broken["first_bad_seq"] is not None and broken["reason"]
    r = runner.invoke(app, ["verify", str(tmp_path / "nope.jsonl"), "--json"])
    assert r.exit_code == 1 and json.loads(r.output)["chain"] == "broken"


def test_reconstruct_json_gives_what_the_page_shows_and_refuses_what_it_refuses(
    tmp_path: Path,
) -> None:
    import json

    with Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec:
        rec.user_message("u", "<b>hi</b>\u202e")
    ledger = tmp_path / "r.jsonl"
    r = runner.invoke(app, ["reconstruct", str(ledger), "--json"])
    assert r.exit_code == 0, r.output
    page = json.loads(r.output)
    assert page["run_id"] == "r" and page["outcome"] == "ok" and page["complete"]
    assert page["total"]["calls"] == 0 and page["models"] == {} and page["tools"] == {}
    assert [row["kind"] for row in page["rows"]] == ["run.start", "user.message", "run.end"]
    shown = page["rows"][1]
    assert "<b>hi</b>" in shown["detail"] and "\u202e" not in shown["detail"]  # printable
    assert not (tmp_path / "r.html").exists()  # prints; writes nothing
    ledger.write_text(ledger.read_text().replace("hi", "ho"))
    r = runner.invoke(app, ["reconstruct", str(ledger), "--json"])
    assert r.exit_code == 1 and "chain broken" in json.loads(r.output)["error"]
    r = runner.invoke(app, ["reconstruct", str(ledger), "--json", "--html"])
    assert r.exit_code == 1 and "--html" in json.loads(r.output)["error"]


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


def _local_run(home: Path, run: str, when: float) -> Path:
    """A finished, signed run as `seatbelt run` leaves it in the data folder."""
    import os

    from seatbelt.attest.sign import keygen

    keys = home / "keys"
    if not (keys / "seatbelt.key").exists():
        keygen(keys)
    signer = Signer.from_file(keys / "seatbelt.key")
    with Recorder.start(
        home / "runs",
        agent_id="gw",
        run_id=f"rh-{run}-1a2b3c4d",
        metadata={"run.name": run},
        signer=signer,
    ) as rec:
        rec.user_message("u", "hi")
    path = home / "runs" / f"rh-{run}-1a2b3c4d.jsonl"
    os.utime(path, (when, when))
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from seatbelt.gateway import launcher

    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(launcher, "LEGACY_CONFIG", tmp_path / "gateway.toml")
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_a_local_run_is_found_by_its_name_or_id_and_checked_against_this_machines_key(
    home: Path,
) -> None:
    older = _local_run(home, "claude-11111111", 1_000_000)
    _local_run(home, "claude-99ce72ff", 2_000_000)
    for ref in (
        "claude-99ce72ff",
        "rh-claude-99ce72ff-1a2b3c4d",
        "rh-claude-99ce72ff-1a2b3c4d.jsonl",
    ):
        r = runner.invoke(app, ["verify", ref])
        assert r.exit_code == 0 and "attested" in r.output, ref  # the machine's key, found
    r = runner.invoke(app, ["reconstruct"])  # no name: the latest run
    assert r.exit_code == 0 and "claude-99ce72ff" in r.output
    r = runner.invoke(app, ["verify", str(older)])  # a path still works
    assert r.exit_code == 0 and "attested" in r.output
    r = runner.invoke(app, ["reconstruct", "claude-nope"])
    assert r.exit_code == 1 and "seatbelt runs" in r.output and "Traceback" not in r.output


def test_seatbelt_runs_json_gives_each_runs_ledger_page_and_state(home: Path) -> None:
    """What a desktop app reads: no table, no markup, every path."""
    import json

    from seatbelt.report import page_path
    from seatbelt.report.html import write_page

    r = runner.invoke(app, ["runs", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["runs"] == []
    done = _local_run(home, "claude-11111111", 1_000_000)
    write_page(done)
    open_ = home / "runs" / "rh-codex-22222222-1a2b3c4d.jsonl"
    Ledger(open_, "rh-codex-22222222-1a2b3c4d").append(
        Kind.RUN_START, Actor(type=ActorType.AGENT, id="gw"), {"run.name": "codex-22222222"}
    )
    os.utime(open_, (2_000_000, 2_000_000))
    broken = home / "runs" / "rh-x-1.jsonl"
    broken.write_text("not json\n")
    os.utime(broken, (3_000_000, 3_000_000))
    r = runner.invoke(app, ["runs", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["folder"] == str(home / "runs") and out["total"] == 3
    unreadable, opened, ended = out["runs"]  # newest first
    assert unreadable == {
        "name": "rh-x-1",
        "id": "rh-x-1",
        "started": None,
        "model_calls": None,
        "status": "unreadable",
        "ok": None,
        "client": None,
        "chain": None,
        "signature": None,
        "ledger": str(broken),
        "page": None,
    }
    assert (opened["name"], opened["status"], opened["ok"], opened["page"]) == (
        "codex-22222222",
        "open",
        None,
        None,
    )
    assert (ended["name"], ended["status"], ended["ok"]) == ("claude-11111111", "ended", True)
    assert ended["page"] == str(page_path(done)) and ended["ledger"] == str(done)
    assert ended["started"] is not None and ended["model_calls"] == 0
    assert (ended["chain"], ended["signature"]) == ("intact", "attested")  # this machine's key
    assert (opened["chain"], opened["signature"]) == ("intact", "unattested")
    done.write_text(done.read_text().replace('"hi"', '"ho"'))
    os.utime(done, (1_000_000, 1_000_000))
    altered = json.loads(runner.invoke(app, ["runs", "--json"]).output)["runs"][-1]
    assert (altered["id"], altered["chain"], altered["signature"]) == (done.stem, "broken", None)
    r = runner.invoke(app, ["runs", "--json", "--limit", "1"])
    assert [x["id"] for x in json.loads(r.output)["runs"]] == ["rh-x-1"]


def test_seatbelt_runs_lists_local_runs_by_name_newest_first(home: Path) -> None:
    r = runner.invoke(app, ["runs"])
    assert r.exit_code == 0 and "No runs recorded yet" in r.output
    _local_run(home, "claude-11111111", 1_000_000)
    _local_run(home, "codex-22222222", 2_000_000)
    r = runner.invoke(app, ["runs"])
    assert r.exit_code == 0
    assert r.output.index("codex-22222222") < r.output.index("claude-11111111")
    assert "2 runs in" in r.output and "seatbelt reconstruct <run>" in r.output
    r = runner.invoke(app, ["reconstruct"])
    assert r.exit_code == 0 and "codex-22222222" in r.output
