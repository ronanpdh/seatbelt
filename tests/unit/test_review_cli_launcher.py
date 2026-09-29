"""Regression tests for the review findings in `seatbelt run`, its local recorder, the
scenario sandbox and the SDK adapters (CLS-*, IER-7, SC-3, SC-8)."""

import asyncio
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from seatbelt import __version__
from seatbelt.attest.manifest import AttestError, sidecar
from seatbelt.attest.sign import KEY_FILE, Signer
from seatbelt.cli import app
from seatbelt.gateway import launcher, local
from seatbelt.gateway.launcher import claude_warnings, environment, load_client_config, run_cli
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
from seatbelt.record.recorder import Recorder
from seatbelt.scenarios import sandbox
from seatbelt.scenarios.sandbox import SandboxError, run_args, run_sandboxed

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def _gateway_config(tmp_path: Path) -> Path:
    path = tmp_path / "gateway-client.toml"
    path.write_text('gateway = "https://gw.corp"\nkey = "sbk_abc"\n')
    path.chmod(0o600)
    return path


def _local_config(tmp_path: Path, text: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


# a CLI that prints which of these variables reached it
ENV_PROBE = "import json, os, sys; print(json.dumps({n: n in os.environ for n in sys.argv[1:]}))"


# CLS-1: the sandbox host reads only a plain file the container left, never through a link


def test_a_symlinked_ledger_is_refused_without_reading_its_target(tmp_path: Path) -> None:
    secret = tmp_path / "credentials"
    secret.write_text("aws_secret_access_key = TOPSECRET\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "s1.jsonl").symlink_to(secret)
    with pytest.raises(SandboxError, match="s1") as caught:
        sandbox._take("s1", scratch / "s1.jsonl", tmp_path / "s1.jsonl", tmp_path / "e.txt")  # pyright: ignore[reportPrivateUsage]
    assert "TOPSECRET" not in str(caught.value)
    assert not (tmp_path / "s1.jsonl").exists()


def test_a_hard_link_directory_or_fifo_is_refused(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real = tmp_path / "real.jsonl"
    real.write_text("{}\n")
    os.link(real, scratch / "hard.jsonl")
    (scratch / "dir.jsonl").mkdir()
    os.mkfifo(scratch / "fifo.jsonl")
    for name in ("hard", "dir", "fifo"):
        with pytest.raises(SandboxError, match=name):
            sandbox._take(  # pyright: ignore[reportPrivateUsage]
                name, scratch / f"{name}.jsonl", tmp_path / f"{name}.jsonl", tmp_path / "e.txt"
            )
        assert not (tmp_path / f"{name}.jsonl").exists()


def test_an_oversized_ledger_is_refused_and_a_plain_one_is_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "big.jsonl").write_bytes(b"x" * 11)
    monkeypatch.setattr(sandbox, "MAX_LEDGER", 10)
    with pytest.raises(SandboxError, match="big"):
        sandbox._take("big", scratch / "big.jsonl", tmp_path / "big.jsonl", tmp_path / "e.txt")  # pyright: ignore[reportPrivateUsage]
    (scratch / "ok.jsonl").write_bytes(b"x" * 10)
    sandbox._take("ok", scratch / "ok.jsonl", tmp_path / "ok.jsonl", tmp_path / "e.txt")  # pyright: ignore[reportPrivateUsage]
    assert (tmp_path / "ok.jsonl").read_bytes() == b"x" * 10
    assert not (tmp_path / "ok.jsonl").is_symlink()


# a docker CLI whose `run` either records the scenario (FAKE_PLANT unset) or leaves a symlink
FAKE_DOCKER = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\\n")
if args[:1] == ["info"]:
    print("99.0"); sys.exit(0)
if args[:2] == ["image", "inspect"]:
    print("sha256:" + "ab" * 32); sys.exit(0)
if "-c" in args:
    print("{version}"); sys.exit(0)
mounts, env = {{}}, dict(os.environ)
for i, a in enumerate(args):
    if a == "-v":
        host, container, *_ = args[i + 1].split(":"); mounts[container] = host
    if a == "-e":
        k, _, v = args[i + 1].partition("="); env[k] = v
if os.environ.get("FAKE_PLANT"):
    spec = json.loads(env["SEATBELT_CHILD"])
    name = spec["scenario"]["id"] + ".jsonl"
    os.symlink(os.environ["FAKE_PLANT"], os.path.join(mounts["/out"], name))
    sys.exit(0)
from seatbelt.scenarios.child import main
sys.exit(main(target_dir=mounts["/target"], out_dir=mounts["/out"], env=env))
"""


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(FAKE_DOCKER.format(python=sys.executable, version=__version__))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "docker.log"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setattr(os, "getuid", lambda: 1000)  # the sandbox refuses root
    return log


def _runs(log: Path) -> list[list[str]]:
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return [c for c in calls if c[:1] == ["run"] and "seatbelt.scenarios.child" in c]


def test_the_sandbox_refuses_a_ledger_the_container_made_a_symlink(
    tmp_path: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "credentials"
    secret.write_text("aws_secret_access_key = TOPSECRET\n")
    monkeypatch.setenv("FAKE_PLANT", str(secret))
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(SandboxError, match="benign-order-status") as caught:
        run_sandboxed(ROOT / "scenarios", "x:y", "img:1", tmp_path / "out", target_dir=target)
    assert "TOPSECRET" not in str(caught.value)
    assert not (tmp_path / "out" / "benign-order-status.jsonl").exists()


# CLS-10: the ledgers folder inside the target directory is hidden from the container


def test_run_args_cover_a_hidden_folder_with_an_empty_read_only_tmpfs(tmp_path: Path) -> None:
    from seatbelt.scenarios.model import load_scenario

    s = load_scenario(ROOT / "scenarios" / "benign-order-status.yaml")
    args = run_args(s, "img:1", tmp_path, tmp_path / "o", "{}", "n", "/target/runs")
    mount = args.index("--mount")
    assert args[mount + 1] == "type=tmpfs,dst=/target/runs,readonly"
    assert args.index(f"{tmp_path.resolve()}:/target:ro") < mount  # over the target's mount
    assert "--mount" not in run_args(s, "img:1", tmp_path, tmp_path / "o", "{}", "n")


def test_ledgers_inside_the_target_dir_are_hidden_and_the_target_itself_refused(
    tmp_path: Path, docker: Path
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "agent.py").write_text("def target(rec, inputs):\n    pass\n")
    run_sandboxed(ROOT / "scenarios", "agent:target", "img:1", target / "runs", target_dir=target)
    runs = _runs(docker)
    assert len(runs) == 8
    assert all("type=tmpfs,dst=/target/runs,readonly" in c for c in runs)
    with pytest.raises(SandboxError, match="--target-dir"):
        run_sandboxed(ROOT / "scenarios", "agent:target", "img:1", target, target_dir=target)
    assert len(_runs(docker)) == 8


def test_the_documented_quick_start_still_runs_with_its_defaults(
    tmp_path: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "scenarios", repo / "scenarios")
    shutil.copytree(ROOT / "examples", repo / "examples")
    monkeypatch.chdir(repo)
    result = runner.invoke(
        app,
        [
            "scenarios",
            "scenarios/",
            "--target",
            "examples.scenario_target:target",
            "--image",
            "seatbelt-target",
        ],
    )
    assert result.exit_code == 1, result.output  # the example target's known finding
    assert "indirect-injection-refund" in result.output
    assert all("type=tmpfs,dst=/target/runs,readonly" in c for c in _runs(docker))
    assert (repo / "runs" / "findings.json").exists()


# CLS-3: a failing tidy is logged, and the sink still catches up


def test_a_failing_tidy_still_lets_the_sink_catch_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caught_up = threading.Event()

    class Shipper:
        def start(self) -> None:
            pass

        def catch_up(self) -> int:
            caught_up.set()
            return 0

        def ship(self, path: Path) -> None:
            pass

        def stop(self, timeout: float) -> int:
            return 0

    def broken(ledgers: Path, signer: Signer) -> list[Path]:
        raise AttestError("attest.json exists; refusing to overwrite")

    def make_sink(cfg: object, env: object) -> Shipper:
        return Shipper()

    monkeypatch.setattr(local, "make_sink", make_sink)
    monkeypatch.setattr(local, "close_dead_runs", broken)
    sink = {"url": "https://s3.example", "bucket": "b", "region": "r"}
    with local.local_recorder(tmp_path / "runs", tmp_path / "keys", "claude-1", sink):
        assert caught_up.wait(10)
    assert "could not close" in caplog.text


# IER-7 / CLS-9: a local run closes only other local runs' ledgers


def _open(ledgers: Path, name: str, attrs: dict[str, Any]) -> Path:
    path = ledgers / f"{name}.jsonl"
    ledger = Ledger(path, name)
    ledger.append(Kind.RUN_START, Actor(type=ActorType.AGENT, id="x"), attrs)
    ledger.append(Kind.USER_MESSAGE, Actor(type=ActorType.USER, id="u"), {"text": "hi"})
    return path


def test_a_local_run_leaves_other_writers_open_ledgers_alone(tmp_path: Path) -> None:
    ledgers = tmp_path / "runs"
    ledgers.mkdir()
    erasure = _open(ledgers, "erasure-1", {"principal.id": "seatbelt:erasure"})
    named_erasure = _open(
        ledgers, "erasure-2", {"principal.id": "seatbelt:erasure", "run.name": "x"}
    )
    imported = _open(ledgers, "import-1", {"principal.id": "user_01"})
    dead = _open(ledgers, "me-claude-dead", {"run.name": "claude-dead"})
    signer = local.local_signer(tmp_path / "keys")
    assert local.close_dead_runs(ledgers, signer) == [dead]
    for path in (erasure, named_erasure, imported):
        assert list(read_events(path))[-1].kind is Kind.USER_MESSAGE
        assert not sidecar(path).exists()


# CLS-4: through a gateway, nothing in the environment takes Claude Code around it


AROUND = [
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_OAUTH_TOKEN",
]


def test_bedrock_vertex_and_oauth_variables_are_removed_through_a_gateway() -> None:
    base = {"PATH": "/bin", **dict.fromkeys(AROUND, "1")}
    env = environment("claude", "https://gw.corp", "sbk_abc", "r", base)
    assert not set(AROUND) & set(env) and env["PATH"] == "/bin"
    kept = environment("claude", "http://127.0.0.1:1/_seatbelt/k/r", "k", "r", base, local=True)
    assert set(AROUND) <= set(kept)  # recording locally, the CLI keeps its own setup


def test_run_cli_warns_about_removed_variables_and_says_what_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-SECRET")
    for status, said in (
        (204, "recorded run claude-"),
        (404, "nothing recorded"),
        (None, "could not end run"),
    ):
        code = run_cli(
            "claude",
            ["-c", ENV_PROBE, *AROUND],
            config=_gateway_config(tmp_path),
            exe=sys.executable,
            end=lambda url, key, run, status=status: status,
        )
        out, err = capfd.readouterr()
        assert code == 0 and not any(json.loads(out).values())
        warning = next(line for line in err.splitlines() if "removed" in line)
        assert "CLAUDE_CODE_USE_BEDROCK" in warning and "CLAUDE_CODE_OAUTH_TOKEN" in warning
        assert "sk-ant-oat-SECRET" not in err
        assert "through the gateway at https://gw.corp" in err
        assert said in err.splitlines()[-1]


def test_claude_settings_that_switch_to_bedrock_or_vertex_are_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    user, cwd = tmp_path / "claude", tmp_path / "project"
    (cwd / ".claude").mkdir(parents=True)
    user.mkdir()
    assert claude_warnings(user, cwd) == []
    (user / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}))
    project = cwd / ".claude" / "settings.json"
    project.write_text(json.dumps({"env": {"CLAUDE_CODE_USE_VERTEX": "true"}}))
    mine, theirs = claude_warnings(user, cwd)
    assert str(user / "settings.json") in mine and "CLAUDE_CODE_USE_BEDROCK" in mine
    assert str(project) in theirs and "CLAUDE_CODE_USE_VERTEX" in theirs
    project.write_text(json.dumps({"env": {"CLAUDE_CODE_USE_VERTEX": "0"}}))
    assert len(claude_warnings(user, cwd)) == 1
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user))
    monkeypatch.chdir(cwd)
    run_cli(
        "claude",
        ["-c", "pass"],
        config=_gateway_config(tmp_path),
        exe=sys.executable,
        end=lambda url, key, run: 404,
    )
    assert str(user / "settings.json") in capfd.readouterr().err


# CLS-5: seatbelt's own secrets never reach the CLI


SEATBELTS = ["SEATBELT_SIGNING_KEY", "SEATBELT_SINK_ACCESS_KEY", "SEATBELT_SINK_SECRET_KEY"]


def test_seatbelts_own_secrets_are_removed_in_both_modes() -> None:
    base = {
        "PATH": "/bin",
        "MY_S3_KEY": "a",
        "ANTHROPIC_API_KEY": "k",
        **dict.fromkeys(SEATBELTS, "s"),
    }
    for is_local in (False, True):
        env = environment("claude", "u", "k", "r", base, is_local, ["MY_S3_KEY"])
        assert not {*SEATBELTS, "MY_S3_KEY"} & set(env) and env["PATH"] == "/bin"


def test_a_local_run_hides_the_sinks_renamed_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))

    def no_sink(cfg: object, env: object) -> None:
        return None  # no object storage here

    monkeypatch.setattr(local, "make_sink", no_sink)
    for name in ("MY_S3_KEY", "MY_S3_SECRET", "SEATBELT_SIGNING_KEY", "SEATBELT_SINK_OTHER"):
        monkeypatch.setenv(name, "secret")
    config = _local_config(
        tmp_path,
        '[sink]\nurl = "https://s3.example"\nbucket = "b"\nregion = "r"\n'
        'access_key_env = "MY_S3_KEY"\nsecret_key_env = "MY_S3_SECRET"\n',
    )
    names = ["MY_S3_KEY", "MY_S3_SECRET", "SEATBELT_SIGNING_KEY", "SEATBELT_SINK_OTHER", "PATH"]
    code = run_cli("claude", ["-c", ENV_PROBE, *names], config=config, exe=sys.executable)
    seen = json.loads(capfd.readouterr().out)
    assert code == 0 and seen == {**dict.fromkeys(names[:-1], False), "PATH": True}


# CLS-6: a stream the caller's code raises in keeps what the model sent


def _client(async_: bool) -> Any:
    anthropic = pytest.importorskip("anthropic")
    import httpx2
    from tests.helpers import sse

    reply = json.loads((ROOT / "tests" / "fixtures" / "anthropic_refund.json").read_text())[0]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(reply)
        )

    transport = httpx2.MockTransport(handler)
    if async_:
        http = anthropic.DefaultAsyncHttpxClient(transport=transport)
        return anthropic.AsyncAnthropic(api_key="test", http_client=http)
    return anthropic.Anthropic(
        api_key="test", http_client=anthropic.DefaultHttpxClient(transport=transport)
    )


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_a_stream_the_caller_raises_in_records_the_response_and_the_error(
    tmp_path: Path, mode: str
) -> None:
    from seatbelt.adapters.anthropic import AnthropicAdapter

    kwargs: dict[str, Any] = {
        "model": "m",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "refund 1001"}],
    }

    async def run_async(rec: Recorder) -> None:
        messages = AnthropicAdapter(rec).async_messages(_client(True))
        async with messages.stream(**kwargs) as stream:
            await stream.get_final_message()
            raise RuntimeError("tool runner failed")

    with (
        Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec,
        pytest.raises(RuntimeError, match="tool runner failed"),
    ):
        if mode == "sync":
            with AnthropicAdapter(rec).messages(_client(False)).stream(**kwargs) as stream:
                stream.get_final_message()
                raise RuntimeError("tool runner failed")
        else:
            asyncio.run(run_async(rec))
    events = list(read_events(tmp_path / "r.jsonl"))
    kinds = [e.kind for e in events]
    assert kinds == [
        Kind.RUN_START,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.RUN_END,
    ]
    response = events[2].attrs
    assert response["gen_ai.response"]["stop_reason"] == "tool_use"
    assert "RuntimeError: tool runner failed" in response["error"]
    assert events[3].attrs["gen_ai.tool.call.id"] == "toolu_01"


# CLS-7: a bad or racing signing key, or a CLI that cannot run, is one line, not a traceback


def test_run_prints_one_line_for_a_bad_key_or_an_unrunnable_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SEATBELT_HOME", str(home))
    monkeypatch.setattr(local, "KEY_WAIT", 0.1)
    config = str(_local_config(tmp_path))
    (home / "keys").mkdir(parents=True)
    (home / "keys" / KEY_FILE).write_text("not a key")
    bad_key = runner.invoke(app, ["run", "claude", "--config", config, "--exe", sys.executable])
    assert bad_key.exit_code == 1 and isinstance(bad_key.exception, SystemExit)
    assert "not a PEM private key" in bad_key.output
    (home / "keys" / KEY_FILE).unlink()
    not_executable = tmp_path / "claude"
    not_executable.write_text("#!/bin/sh\n")
    not_executable.chmod(0o644)
    denied = runner.invoke(app, ["run", "claude", "--config", config, "--exe", str(not_executable)])
    assert denied.exit_code == 1 and isinstance(denied.exception, SystemExit), denied.output
    assert "Permission denied" in denied.output and "Traceback" not in denied.output


def test_a_first_run_that_loses_the_key_race_reads_the_winners_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = tmp_path / "keys"
    winner = Signer.generate()

    def lose(directory: Path) -> tuple[Path, Path]:
        """The other run made the key file first and has not written it yet."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / KEY_FILE).write_bytes(b"")
        timer = threading.Timer(0.2, (directory / KEY_FILE).write_bytes, [winner.private_pem()])
        timer.start()
        raise AttestError(f"{directory / KEY_FILE} exists; refusing to overwrite")

    monkeypatch.setattr(local, "keygen", lose)
    signer = local.local_signer(keys)
    assert signer.private_pem() == winner.private_pem()


# CLS-8: exit statuses as a shell gives them, and SIGTERM reaches the CLI


def test_a_cli_killed_by_a_signal_exits_128_plus_its_number(tmp_path: Path) -> None:
    before = signal.getsignal(signal.SIGTERM)
    code = run_cli(
        "claude",
        ["-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
        config=_gateway_config(tmp_path),
        exe=sys.executable,
        end=lambda url, key, run: 404,
    )
    assert code == 128 + signal.SIGKILL
    assert signal.getsignal(signal.SIGTERM) is before  # restored


SEATBELT = (
    "import sys; from seatbelt.gateway import launcher; launcher.GRACE = float(sys.argv.pop(1)); "
    "from seatbelt.cli import app; app()"
)
SLOW_CLI = (
    "import pathlib, signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN) if sys.argv[2] == 'stubborn' else None; "
    "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize(("cli", "status"), [("polite", 143), ("stubborn", 137)])
def test_sigterm_to_seatbelt_reaches_the_cli_and_the_run_is_closed(
    tmp_path: Path, cli: str, status: int
) -> None:
    home, ready = tmp_path / "home", tmp_path / "ready"
    env = {**os.environ, "SEATBELT_HOME": str(home)}
    config = _local_config(tmp_path)
    command = [sys.executable, "-c", SEATBELT, "0.5", "run", "claude", "--config", str(config)]
    command += ["--exe", sys.executable, "--", "-c", SLOW_CLI, str(ready), cli]
    seatbelt = subprocess.Popen(command, env=env, stderr=subprocess.PIPE, text=True)  # noqa: S603
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert seatbelt.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        seatbelt.send_signal(signal.SIGTERM)
        _, err = seatbelt.communicate(timeout=30)
    finally:
        seatbelt.kill()
    assert seatbelt.returncode == status, err
    assert "nothing recorded" in err  # the run's finally blocks ran
    assert list((home / "runs" / ".running").iterdir()) == []


# CLS-11: the Agents SDK processor forgets an agent span once it ends


def test_an_ended_agent_span_is_forgotten(tmp_path: Path) -> None:
    pytest.importorskip("agents")
    from agents.tracing import AgentSpanData, Span

    from seatbelt.adapters.openai_agents import SeatbeltProcessor

    class FakeSpan:
        def __init__(self, span_id: str) -> None:
            self.span_data = AgentSpanData(f"agent-{span_id}")
            self.span_id, self.parent_id, self.trace_id = span_id, None, "t"

    with Recorder.start(tmp_path, agent_id="a", run_id="r") as rec:
        proc = SeatbeltProcessor(rec)
        for i in range(3):
            agent = cast(Span[Any], FakeSpan(f"s{i}"))
            proc.on_span_start(agent)
            proc.on_span_end(agent)
    assert proc._agents == {}  # pyright: ignore[reportPrivateUsage]


# SC-3: an ignored gateway.toml is named, and a run says where it records


def test_a_gateway_toml_that_config_toml_shadows_is_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(launcher, "LEGACY_CONFIG", tmp_path / "gateway.toml")
    (tmp_path / "gateway.toml").write_text('url = "https://gw.corp"\nkey = "sbk_abc"\n')
    (tmp_path / "config.toml").write_text('ledgers = "runs"\n')
    assert load_client_config().gateway is None
    err = capsys.readouterr().err
    assert str(tmp_path / "gateway.toml") in err and "recorded on this machine" in err
    (tmp_path / "config.toml").write_text('gateway = "https://gw.corp"\nkey = "sbk_abc"\n')
    assert load_client_config().gateway == "https://gw.corp"
    assert capsys.readouterr().err == ""
    assert "sbk_abc" not in err


def test_a_run_says_whether_it_records_locally_or_through_a_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    config = _local_config(tmp_path)
    assert run_cli("claude", ["-c", "pass"], config=config, exe=sys.executable) == 0
    err = capfd.readouterr().err
    assert f"seatbelt: recording claude on this machine (no gateway in {config})" in err
    run_cli(
        "claude",
        ["-c", "pass"],
        config=_gateway_config(tmp_path),
        exe=sys.executable,
        end=lambda url, key, run: 204,
    )
    assert (
        "seatbelt: recording claude through the gateway at https://gw.corp"
        in capfd.readouterr().err
    )


# SC-8: the demo says how to check and replay the run it wrote


def test_demo_prints_the_commands_that_find_its_run(tmp_path: Path) -> None:
    result = runner.invoke(app, ["demo", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    (ledger,) = tmp_path.glob("*.jsonl")
    assert f"seatbelt verify {ledger}" in result.output
    assert f"seatbelt reconstruct {ledger}" in result.output
    assert runner.invoke(app, ["verify", str(ledger)]).exit_code == 0
