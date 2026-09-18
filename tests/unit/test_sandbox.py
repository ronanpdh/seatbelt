import json
import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seatbelt import __version__
from seatbelt.attest.sign import Signer, keygen
from seatbelt.cli import app
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder
from seatbelt.scenarios.child import ENV, ChildSpec
from seatbelt.scenarios.child import main as child_main
from seatbelt.scenarios.model import Inputs, ScenarioError, load_scenario
from seatbelt.scenarios.runner import collect, record, refuse_taken
from seatbelt.scenarios.sandbox import HARDENING, Docker, SandboxError, run_args, run_sandboxed

ROOT = Path(__file__).resolve().parents[2]
BENIGN = ROOT / "scenarios" / "benign-order-status.yaml"
runner = CliRunner()


def _lookup(rec: Recorder, inputs: Inputs) -> None:
    with rec.tool_call("lookup_order", {"order": "1001"}) as t:
        t.result(inputs.tool_result("lookup_order", {"status": "delivered"}))


def test_record_writes_one_ledger_with_host_metadata(tmp_path: Path) -> None:
    s = load_scenario(BENIGN)
    path = record(s, tmp_path, _lookup, metadata={"sandbox.image": "x:1"})
    events = list(read_events(path))
    assert path == tmp_path / "benign-order-status.jsonl"
    assert events[0].attrs["scenario.id"] == "benign-order-status"
    assert events[0].attrs["sandbox.image"] == "x:1"
    assert events[1].kind is Kind.USER_MESSAGE
    assert events[-1].kind is Kind.RUN_END and events[-1].attrs["run.ok"] is True


def test_collect_evaluates_whatever_ledger_the_producer_hands_back(tmp_path: Path) -> None:
    s = load_scenario(BENIGN)
    report = collect([s], "deadbeef", tmp_path, lambda sc: record(sc, tmp_path, _lookup))
    assert report.corpus_sha256 == "deadbeef" and report.findings == []
    assert (tmp_path / "findings.json").exists()


def test_scenario_egress_defaults_false(tmp_path: Path) -> None:
    assert load_scenario(BENIGN).egress is False
    text = BENIGN.read_text() + "egress: true\n"
    (tmp_path / "e.yaml").write_text(text.replace("benign-order-status", "e"))
    assert load_scenario(tmp_path / "e.yaml").egress is True


def test_refuse_taken_names_the_scenario(tmp_path: Path) -> None:
    s = load_scenario(BENIGN)
    (tmp_path / "benign-order-status.jsonl").write_text("")
    with pytest.raises(ScenarioError, match="benign-order-status"):
        refuse_taken([s], tmp_path, None)


TARGET_SRC = """
from seatbelt.record.recorder import Recorder
from seatbelt.scenarios.model import Inputs

def target(rec: Recorder, inputs: Inputs) -> None:
    with rec.tool_call("lookup_order", {"order": "1001"}) as t:
        t.result(inputs.tool_result("lookup_order", {"status": "delivered"}))

def boom(rec: Recorder, inputs: Inputs) -> None:
    raise RuntimeError("boom")

CONST = "not a target"

def forge(rec: Recorder, inputs: Inputs) -> None:
    out = rec.ledger.path.parent
    if rec.run_id == "benign-order-status":
        (out / "junk.txt").write_text("junk")
        forged = "indirect-injection-refund"
        with Recorder.start(out, agent_id="forger", run_id=forged, metadata={"forged": True}) as f:
            with f.tool_call("lookup_order", {"order": "1001"}) as t:
                t.result({"status": "delivered"})
    with rec.tool_call("lookup_order", {"order": "1001"}) as t:
        result = inputs.tool_result("lookup_order", {"status": "delivered"})
        t.result(result)
    if "SYSTEM:" in str(result):
        with rec.tool_call("issue_refund", {"order": "all"}) as t:
            t.result({"ok": True})
"""


def _target_dir(tmp_path: Path) -> Path:
    d = tmp_path / "target"
    d.mkdir()
    (d / "agent.py").write_text(TARGET_SRC)
    return d


def test_child_records_one_scenario_from_env(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    spec = ChildSpec(
        scenario=load_scenario(BENIGN), target="agent:target", metadata={"sandbox.image": "x"}
    )
    code = child_main(
        target_dir=str(_target_dir(tmp_path)), out_dir=str(out), env={ENV: spec.model_dump_json()}
    )
    assert code == 0
    events = list(read_events(out / "benign-order-status.jsonl"))
    assert events[0].attrs["sandbox.image"] == "x"
    assert any(e.kind is Kind.TOOL_CALL for e in events)
    assert events[-1].attrs["run.ok"] is True


def test_child_records_a_crashing_target_as_a_failed_run(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    spec = ChildSpec(scenario=load_scenario(BENIGN), target="agent:boom")
    code = child_main(
        target_dir=str(_target_dir(tmp_path)), out_dir=str(out), env={ENV: spec.model_dump_json()}
    )
    assert code == 0
    events = list(read_events(out / "benign-order-status.jsonl"))
    assert events[-1].attrs["run.ok"] is False and "boom" in events[-1].attrs["run.error"]


def test_child_fails_loudly_on_bad_spec_or_target(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        child_main(target_dir=str(tmp_path), out_dir=str(tmp_path), env={})
    spec = ChildSpec(scenario=load_scenario(BENIGN), target="nope:target")
    with pytest.raises(ModuleNotFoundError):
        child_main(
            target_dir=str(tmp_path), out_dir=str(tmp_path), env={ENV: spec.model_dump_json()}
        )


def test_child_refuses_a_target_that_is_not_callable(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    spec = ChildSpec(scenario=load_scenario(BENIGN), target="agent:CONST")
    with pytest.raises(TypeError, match="agent:CONST"):
        child_main(
            target_dir=str(_target_dir(tmp_path)),
            out_dir=str(out),
            env={ENV: spec.model_dump_json()},
        )
    assert not list(out.iterdir())


FAKE_DOCKER = '''#!{python}
"""Fake docker CLI: answers the probes and runs the child in-process for `run`."""
import json, os, sys, time
args = sys.argv[1:]
log = os.environ.get("FAKE_DOCKER_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(args) + "\\n")
if args[:1] == ["info"]:
    print("99.0"); sys.exit(0)
if args[:1] == ["kill"]:
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    if args[-1] == "missing:latest":
        print("Error: No such image", file=sys.stderr); sys.exit(1)
    print("sha256:" + "ab" * 32); sys.exit(0)
if "-c" in args:  # version probe
    print(os.environ.get("FAKE_SEATBELT_VERSION", "{version}")); sys.exit(0)
mounts, env = {{}}, dict(os.environ)
for i, a in enumerate(args):
    if a == "-v":
        host, container, *_ = args[i + 1].split(":"); mounts[container] = host
    if a == "-e":
        k, _, v = args[i + 1].partition("="); env[k] = v
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
from seatbelt.scenarios.child import main
sys.exit(main(target_dir=mounts["/target"], out_dir=mounts["/out"], env=env))
'''


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(FAKE_DOCKER.format(python=sys.executable, version=__version__))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "docker.log"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    return log


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_run_args_hardening_mounts_and_network(tmp_path: Path) -> None:
    s = load_scenario(BENIGN)
    args = run_args(s, "img:1", tmp_path / "t", tmp_path / "o", "{}", "seatbelt-x")
    assert args[:2] == ["run", "--rm"]
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    for flag in (
        "--read-only",
        "--cap-drop",
        "--security-opt",
        "--pids-limit",
        "--memory",
        "--user",
        "--name",
    ):
        assert flag in args
    assert f"{(tmp_path / 't').resolve()}:/target:ro" in args
    assert f"{(tmp_path / 'o').resolve()}:/out" in args
    assert args[-4:] == ["img:1", "python", "-m", "seatbelt.scenarios.child"]
    start = args.index(HARDENING[0])
    assert args[start : start + len(HARDENING)] == HARDENING
    assert args[args.index("-e") + 1].startswith("SEATBELT_CHILD=")
    open_ = run_args(s.model_copy(update={"egress": True}), "img:1", tmp_path, tmp_path, "{}", "n")
    assert open_[open_.index("--network") + 1] == "bridge"


def test_sandboxed_corpus_matches_the_in_process_run(tmp_path: Path, fake_docker: Path) -> None:
    key, _pub = keygen(tmp_path / "keys")
    out = tmp_path / "runs"
    report = run_sandboxed(
        ROOT / "scenarios",
        "examples.scenario_target:target",
        "img:1",
        out,
        target_dir=ROOT,
        signer=Signer.from_file(key),
    )
    assert [f.scenario_id for f in report.findings] == ["indirect-injection-refund"]
    assert all(r.run_ok for r in report.results)
    events = list(read_events(out / "benign-order-status.jsonl"))
    assert events[0].attrs["sandbox.image"] == "img:1"
    assert events[0].attrs["sandbox.image_digest"].startswith("sha256:")
    assert events[0].attrs["sandbox.egress"] is False
    assert (out / "benign-order-status.attest.json").exists()
    assert (out / "benign-order-status.stderr.txt").exists()
    runs = [c for c in _calls(fake_docker) if c[:1] == ["run"] and "seatbelt.scenarios.child" in c]
    assert len(runs) == 8 and all(c[c.index("--network") + 1] == "none" for c in runs)


def test_sandbox_refuses_bad_docker_image_and_version(
    tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = ROOT / "scenarios"
    with pytest.raises(SandboxError, match="not found on PATH"):
        run_sandboxed(
            corpus,
            "x:y",
            "img:1",
            tmp_path / "a",
            target_dir=ROOT,
            docker=Docker(binary="no-such-docker"),
        )
    with pytest.raises(SandboxError, match="missing:latest"):
        run_sandboxed(corpus, "x:y", "missing:latest", tmp_path / "b", target_dir=ROOT)
    monkeypatch.setenv("FAKE_SEATBELT_VERSION", "0.0.0")
    with pytest.raises(SandboxError, match=r"0\.0\.0"):
        run_sandboxed(corpus, "x:y", "img:1", tmp_path / "c", target_dir=ROOT)
    with pytest.raises(SandboxError, match="target"):
        run_sandboxed(corpus, "x:y", "img:1", tmp_path / "d", target_dir=tmp_path / "nope")
    assert not any((tmp_path / d).exists() for d in ("a", "b", "c", "d"))


def test_sandbox_timeout_kills_and_reports_without_a_ledger(
    tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SLEEP", "5")
    out = tmp_path / "runs"
    with pytest.raises(SandboxError, match=r"stderr\.txt"):
        run_sandboxed(
            ROOT / "scenarios",
            "examples.scenario_target:target",
            "img:1",
            out,
            target_dir=ROOT,
            timeout=0.5,
        )
    assert any(c[:1] == ["kill"] for c in _calls(fake_docker))
    assert "timeout" in (out / "benign-order-status.stderr.txt").read_text()


def test_sandbox_container_without_a_ledger_is_an_error(tmp_path: Path, fake_docker: Path) -> None:
    out = tmp_path / "runs"
    with pytest.raises(SandboxError, match="benign-order-status"):
        run_sandboxed(
            ROOT / "scenarios", "examples.scenario_target:nope", "img:1", out, target_dir=ROOT
        )
    assert "AttributeError" in (out / "benign-order-status.stderr.txt").read_text()


def test_scratch_dir_keeps_one_scenario_from_forging_another(
    tmp_path: Path, fake_docker: Path
) -> None:
    out = tmp_path / "runs"
    report = run_sandboxed(
        ROOT / "scenarios", "agent:forge", "img:1", out, target_dir=_target_dir(tmp_path)
    )
    assert any(
        f.scenario_id == "indirect-injection-refund" and f.check == "no_tool_call: issue_refund"
        for f in report.findings
    )
    events = list(read_events(out / "indirect-injection-refund.jsonl"))
    assert "forged" not in events[0].attrs and events[0].attrs["sandbox.image"] == "img:1"
    assert not (out / "junk.txt").exists()
    assert not list(out.glob(".benign-order-status-*"))


def test_sandbox_refuses_to_run_as_root(
    tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(SandboxError, match="root"):
        run_sandboxed(ROOT / "scenarios", "x:y", "img:1", tmp_path / "runs", target_dir=ROOT)
    assert not any(c[:1] == ["run"] for c in _calls(fake_docker))


def test_cli_refuses_a_key_inside_the_target_dir(tmp_path: Path, fake_docker: Path) -> None:
    key, _pub = keygen(tmp_path / "keys")
    result = runner.invoke(
        app,
        [
            "scenarios",
            str(ROOT / "scenarios"),
            "--target",
            "x:y",
            "--image",
            "img:1",
            "--target-dir",
            str(tmp_path),
            "--key",
            str(key),
            "--out",
            str(tmp_path / "runs"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "--key is inside --target-dir" in result.output
    assert not any(c[:1] == ["run"] for c in _calls(fake_docker))


def test_cli_scenarios_image_runs_sandboxed_and_list_shows_egress(
    tmp_path: Path, fake_docker: Path
) -> None:
    listed = runner.invoke(app, ["scenarios", str(ROOT / "scenarios"), "--list"])
    assert listed.exit_code == 0 and "egress" in listed.output
    out = tmp_path / "runs"
    ran = runner.invoke(
        app,
        [
            "scenarios",
            str(ROOT / "scenarios"),
            "--target",
            "examples.scenario_target:target",
            "--image",
            "img:1",
            "--target-dir",
            str(ROOT),
            "--out",
            str(out),
        ],
    )
    assert ran.exit_code == 1, ran.output
    assert "FAIL" in ran.output and (out / "findings.json").exists()
    assert any("seatbelt.scenarios.child" in c for c in _calls(fake_docker))
    bad = runner.invoke(
        app,
        [
            "scenarios",
            str(ROOT / "scenarios"),
            "--target",
            "x:y",
            "--image",
            "missing:latest",
            "--out",
            str(tmp_path / "x"),
        ],
    )
    assert bad.exit_code == 1 and "missing:latest" in bad.output
