import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, keygen
from seatbelt.cli import app
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger
from seatbelt.record.recorder import Recorder
from seatbelt.report.fleet import People, fleet

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


def test_anthropic_prompt_cache_tokens_are_reported_beside_input(tmp_path: Path) -> None:
    """Anthropic's input_tokens leaves out its prompt cache, so with caching `in` alone shows
    a few tokens for a prompt of thousands."""
    runs = tmp_path / "runs"
    with Recorder.start(
        runs, "gateway", run_id="r1", metadata={"principal.id": "dana@corp"}
    ) as rec:
        for read, written in ((0, 150_000), (150_000, 200)):
            call = rec.model_requested("claude-opus-5-5", {"messages": []})
            usage = {
                "input_tokens": 3,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": written,
                "output_tokens": 9,
            }
            call.respond({}, usage, response_model="claude-opus-5-5")
    dana = fleet(runs).by_principal["dana@corp"]
    assert (dana.input_tokens, dana.cache_read_input_tokens) == (6, 150_000)
    assert dana.cache_creation_input_tokens == 150_200
    runner = CliRunner()
    r = runner.invoke(app, ["report", str(runs)])
    assert r.exit_code == 0 and "cache" in r.output
    assert "150000" in r.output and "150200" in r.output
    r = runner.invoke(app, ["report", str(runs), "--json"])
    model = json.loads(r.output)["by_model"]["claude-opus-5-5"]
    assert model["cache_read_input_tokens"] == 150_000
    assert model["cache_creation_input_tokens"] == 150_200


def test_cli_report_with_no_directory_reads_this_machines_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from seatbelt.gateway import launcher

    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(launcher, "LEGACY_CONFIG", tmp_path / "gateway.toml")
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    runner = CliRunner()
    r = runner.invoke(app, ["report"])
    assert r.exit_code == 0 and "No runs recorded yet" in r.output
    keygen(tmp_path / "home" / "keys")
    signer = Signer.from_file(tmp_path / "home" / "keys" / "seatbelt.key")
    with Recorder.start(tmp_path / "home" / "runs", agent_id="gw", signer=signer) as rec:
        rec.user_message("u", "hi")
    r = runner.invoke(app, ["report"])
    assert r.exit_code == 0 and "1 runs" in r.output and "unattested" not in r.output


def test_cli_report_with_a_bad_client_config_says_so_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from seatbelt.gateway import launcher

    (tmp_path / "config.toml").write_text('ledger = "typo"\n')
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", tmp_path / "config.toml")
    r = CliRunner().invoke(app, ["report"])
    assert r.exit_code == 1 and "unknown keys ledger" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def _people(root: Path, text: str) -> Path:
    path = root / "people.yaml"
    path.write_text(text)
    return path


def test_a_people_file_joins_one_persons_ids_across_directories(tmp_path: Path) -> None:
    """A person seen through the gateway (an issued key) and through the Compliance API
    importer (an Anthropic user id) is one row once the file names both ids."""
    _build(tmp_path)
    imported = tmp_path / "runs" / "compliance"
    with Recorder.start(
        imported, "compliance:cowork", run_id="i1", metadata={"principal.id": "user_01Gp"}
    ) as rec:
        rec.model_responded("claude-opus-5-5", {"content": []})
    people = People.load(
        _people(
            tmp_path, "people:\n  Alice:\n    - alice@corp\n    - user_01Gp\n    - never-seen\n"
        )
    )
    report = fleet([tmp_path / "runs", imported], people=people)
    alice = report.by_principal["Alice"]
    assert (alice.runs, alice.calls) == (3, 3)  # two gateway runs, one imported
    assert "alice@corp" not in report.by_principal and "user_01Gp" not in report.by_principal
    assert report.by_principal["bob@corp"].runs == 1  # not in the file: as recorded
    assert report.people == {"Alice": ["alice@corp", "user_01Gp"]}  # ids seen, not all listed
    assert fleet(tmp_path / "runs").people == {}  # no file: nothing joined


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("people:\n  A: [x]\n  B: [x]\n", "x is listed for both A and B"),
        ("people:\n  A: x\n", "expected a list"),
        ("people:\n  A: [1]\n", "expected a list"),
        ("A: [x]\n", "expected `people:`"),
        ("people: [\n", "people.yaml"),
    ],
)
def test_a_bad_people_file_is_refused(tmp_path: Path, text: str, error: str) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        People.load(_people(tmp_path, text))


def test_cli_report_with_a_people_file(tmp_path: Path) -> None:
    _build(tmp_path)
    people = _people(tmp_path, "people:\n  Alice Example:\n    - alice@corp\n")
    runner = CliRunner()
    r = runner.invoke(app, ["report", str(tmp_path / "runs"), "--people", str(people)])
    assert "Alice Example" in r.output and "Alice Example: alice@corp" in r.output
    bad = _people(tmp_path, "people:\n  A: [x]\n  B: [x]\n")
    r = runner.invoke(app, ["report", str(tmp_path / "runs"), "--people", str(bad)])
    assert r.exit_code == 1 and "listed for both" in r.output
