import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, keygen
from seatbelt.cli import app
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger
from seatbelt.record.recorder import Recorder
from seatbelt.report.fleet import fleet

MODEL = "claude-sonnet-5-20260601"


def _build(root: Path) -> tuple[Signer, dict[str, str]]:
    keygen(root / "keys")
    signer = Signer.from_file(root / "keys" / "seatbelt.key")
    runs = root / "runs"
    ids: dict[str, str] = {}
    with Recorder.start(
        runs, "gateway", run_id="a1", metadata={"principal.id": "alice@corp"}, signer=signer
    ) as rec:
        for n in (10, 20):
            call = rec.model_requested("claude-sonnet-5", {"messages": []})
            call.respond({}, {"input_tokens": n, "output_tokens": 1}, response_model=MODEL)
        rec.tool_called("lookup_order", {"order_id": 1})
        refused = rec.model_requested("claude-opus-5", {"messages": []})
        rec.policy_check("models", refused.request.id, False, "model claude-opus-5 is not allowed")
    ids["signed"] = "a1"
    with (
        pytest.raises(RuntimeError),
        Recorder.start(
            runs, "gateway", run_id="a2", metadata={"principal.id": "alice@corp"}
        ) as rec,
    ):
        raise RuntimeError("crashed")  # failed and unsigned
    with Recorder.start(
        runs, "gateway", run_id="b1", metadata={"principal.id": "bob@corp"}, signer=signer
    ) as rec:
        call = rec.model_requested("gpt-5", {"messages": []})
        call.respond({}, {"input_tokens": 7, "output_tokens": 3}, response_model="gpt-5")
        rec.tool_called("lookup_order", {"order_id": 2})
    Ledger(runs / "c1.jsonl", "c1").append(  # started, never closed
        Kind.RUN_START, Actor(type=ActorType.SYSTEM, id="seatbelt"), {"principal.id": "carol@corp"}
    )
    with Recorder.start(
        runs, "gateway", run_id="t1", metadata={"principal.id": "mallory@corp"}
    ) as rec:
        call = rec.model_requested("m", {})
        call.respond({}, {"input_tokens": 999})
    tampered = runs / "t1.jsonl"
    tampered.write_text(tampered.read_text().replace(":999", ":1"))
    return signer, ids


def test_fleet_totals(tmp_path: Path) -> None:
    _build(tmp_path)
    report = fleet(tmp_path / "runs")
    assert report.runs == 4  # the tampered one is not counted
    alice = report.by_principal["alice@corp"]
    assert (alice.runs, alice.calls, alice.input_tokens, alice.denials) == (2, 2, 30, 1)
    assert report.by_model[MODEL].calls == 2 and report.by_model[MODEL].runs == 1
    assert report.by_model["claude-opus-5"].denials == 1  # counted against what was asked for
    assert report.by_tool == {"lookup_order": 2}
    assert report.failed == ["a2"] and report.incomplete == ["c1"]
    assert report.unattested == ["a2", "c1"] and report.broken == ["t1"]
    assert "mallory@corp" not in report.by_principal
    assert json.loads(report.model_dump_json())["by_tool"] == {"lookup_order": 2}


def test_fleet_with_a_key_finds_forged_attestations(tmp_path: Path) -> None:
    _build(tmp_path)
    side = sidecar(tmp_path / "runs" / "b1.jsonl")
    manifest = json.loads(side.read_text())
    manifest["events"] += 1
    side.write_text(json.dumps(manifest))
    report = fleet(tmp_path / "runs", tmp_path / "keys" / "seatbelt.pub")
    assert report.forged == ["b1"]


def test_cli_report(tmp_path: Path) -> None:
    _build(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["report", str(tmp_path / "runs"), "--json"])
    assert r.exit_code == 1  # a broken ledger is tamper evidence
    assert json.loads(r.output)["by_principal"]["bob@corp"]["input_tokens"] == 7
    r = runner.invoke(app, ["report", str(tmp_path / "runs")])
    assert "alice@corp" in r.output and "lookup_order" in r.output and "broken" in r.output
