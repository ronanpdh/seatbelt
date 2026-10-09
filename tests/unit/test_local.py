"""`seatbelt run` recording on this machine: the gateway starts inside the run, the CLI keeps
its own credentials, and the ledger lands in the local data folder, signed."""

import ast
import json
import shlex
import stat
import sys
import threading
import tomllib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import fake_claude

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import PUB_FILE
from seatbelt.gateway.launcher import data_dir, local_defaults, run_cli
from seatbelt.gateway.local import UPSTREAMS
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.report import page_path

REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


class Provider:
    """A stand-in provider on localhost that notes what reached it."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["content-length"]))
                seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
                out = json.dumps(REPLY).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def provider() -> Iterator[Provider]:
    p = Provider()
    yield p
    p.server.shutdown()


# a CLI that does what Claude Code does with the preset: its own key, sent to the base URL
CLAUDE = """
import json, os, sys, urllib.request
headers = {"content-type": "application/json", "x-api-key": "sk-ant-USERS-OWN",
           "anthropic-version": "2023-06-01"}
BASE = os.environ["ANTHROPIC_BASE_URL"]
body = {"model": "claude-sonnet-5", "max_tokens": 5,
        "messages": [{"role": "user", "content": "hi"}]}
req = urllib.request.Request(BASE + "/v1/messages?beta=true",
                             data=json.dumps(body).encode(), headers=headers)
print(json.load(urllib.request.urlopen(req))["content"][0]["text"])
sys.exit(7)
"""


def _config(tmp_path: Path, provider: Provider) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[upstreams]\nanthropic = "{provider.url}"\n')
    return path


def test_a_local_run_passes_the_clis_own_key_through_and_signs_the_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-USERS-OWN")
    config, claude = _config(tmp_path, provider), fake_claude(tmp_path)
    code = run_cli("claude", ["-c", CLAUDE], config=config, exe=claude)
    out, err = capfd.readouterr()
    assert code == 7 and out.strip() == "hi"  # the CLI's exit code and output
    (sent,) = provider.seen
    assert sent["path"] == "/v1/messages?beta=true"
    headers = {k.lower(): v for k, v in sent["headers"].items()}
    assert headers["x-api-key"] == "sk-ant-USERS-OWN"  # passed through, not replaced
    assert "x-seatbelt-key" not in headers and "x-seatbelt-run" not in headers
    (ledger,) = (tmp_path / "home" / "runs").glob("*.jsonl")
    run = ledger.stem.split("-", 1)[1].rsplit("-", 1)[0]  # <user>-<run>-<hex>
    assert f"[seatbelt] recorded run {run}" in err
    assert f"replay it: seatbelt reconstruct {run}" in err and str(ledger) in err
    events = list(read_events(ledger))
    assert [e.kind for e in events] == [
        Kind.RUN_START,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.RUN_END,
    ]
    assert events[0].attrs["run.name"].startswith("claude-")
    assert "sk-ant-USERS-OWN" not in ledger.read_text()  # credentials are never recorded
    assert sidecar(ledger).exists() and (tmp_path / "home" / "keys" / PUB_FILE).exists()


def test_the_ledgers_path_is_printed_ready_to_paste_into_a_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The macOS data folder is ~/Library/Application Support/seatbelt. Unquoted, a shell
    splits the path at the space, and an editor opens two empty files that do not exist."""
    home = tmp_path / "Application Support" / "seatbelt"
    monkeypatch.setenv("SEATBELT_HOME", str(home))
    run_cli("claude", ["-c", CLAUDE], config=_config(tmp_path, provider), exe=fake_claude(tmp_path))
    (ledger,) = (home / "runs").glob("*.jsonl")
    lines = [x.removeprefix("[seatbelt]").strip() for x in capfd.readouterr().err.splitlines()]
    (line,) = [x for x in lines if x.startswith("file:")]
    assert shlex.split(line.removeprefix("file:")) == [str(ledger)]


def test_a_local_run_writes_its_page_beside_the_ledger_and_says_where(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    capfd: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "Application Support" / "seatbelt"
    monkeypatch.setenv("SEATBELT_HOME", str(home))
    run_cli("claude", ["-c", CLAUDE], config=_config(tmp_path, provider), exe=fake_claude(tmp_path))
    (ledger,) = (home / "runs").glob("*.jsonl")
    page = page_path(ledger)
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    text = page.read_text()
    assert "attested" in text and "claude-sonnet-5" in text  # checked against this machine's key
    lines = [x.removeprefix("[seatbelt]").strip() for x in capfd.readouterr().err.splitlines()]
    (line,) = [x for x in lines if x.startswith("page:")]
    assert shlex.split(line.removeprefix("page:")) == [str(page)]


def test_a_local_run_reports_its_ledger_page_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    report = tmp_path / "report.json"
    monkeypatch.setenv("SEATBELT_RUN_REPORT", str(report))
    run_cli("claude", ["-c", CLAUDE], config=_config(tmp_path, provider), exe=fake_claude(tmp_path))
    (ledger,) = (tmp_path / "home" / "runs").glob("*.jsonl")
    data = json.loads(report.read_text())
    assert (data["recorded_by"], data["recorded"], data["exit"]) == ("local", True, 7)
    assert data["ledgers"] == [str(ledger)] and data["pages"] == [str(page_path(ledger))]
    assert data["run"] == next(iter(read_events(ledger))).attrs["run.name"]


def test_a_quiet_run_says_only_its_warnings_and_still_records_and_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """SEATBELT_QUIET: for a host, such as the desktop app, that shows the run its own way."""
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SEATBELT_QUIET", "1")
    report = tmp_path / "report.json"
    monkeypatch.setenv("SEATBELT_RUN_REPORT", str(report))
    code = run_cli(
        "claude", ["-c", CLAUDE], config=_config(tmp_path, provider), exe=fake_claude(tmp_path)
    )
    out, err = capfd.readouterr()
    assert code == 7 and out.strip() == "hi"
    assert "recording claude" not in err and "recorded run" not in err
    (ledger,) = (tmp_path / "home" / "runs").glob("*.jsonl")
    assert sidecar(ledger).exists()
    assert json.loads(report.read_text())["recorded"] is True


def test_the_quiet_setting_is_seatbelts_own_and_not_passed_to_the_cli() -> None:
    from seatbelt.gateway.launcher import _seatbelts

    assert _seatbelts("SEATBELT_QUIET", ())
    assert not _seatbelts("SEATBELT_NO_ANIMATION", ())


def test_html_false_in_the_config_writes_no_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    config = _config(tmp_path, provider)
    config.write_text("html = false\n" + config.read_text())
    run_cli("claude", ["-c", CLAUDE], config=config, exe=fake_claude(tmp_path))
    (ledger,) = (tmp_path / "home" / "runs").glob("*.jsonl")
    assert sidecar(ledger).exists() and not page_path(ledger).exists()


def test_a_page_that_cannot_be_written_does_not_fail_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    capfd: pytest.CaptureFixture[str],
) -> None:
    from seatbelt.gateway import local

    def broken(*_: object, **__: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(local, "write_page", broken)
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    code = run_cli(
        "claude", ["-c", CLAUDE], config=_config(tmp_path, provider), exe=fake_claude(tmp_path)
    )
    assert code == 7  # the CLI's own exit code, as without the page
    (ledger,) = (tmp_path / "home" / "runs").glob("*.jsonl")
    assert sidecar(ledger).exists() and not page_path(ledger).exists()
    err = capfd.readouterr().err
    assert "no HTML page for" in err and "disk full" in err and "page:" not in err


ANTHROPIC = UPSTREAMS["anthropic"]
TOOL_SEARCH_PROBE = "import os; print(os.environ.get('ENABLE_TOOL_SEARCH'))"


@pytest.mark.parametrize(
    ("upstream", "base_url", "own", "expected"),
    [
        (None, None, None, "true"),  # forwarded to Anthropic: as without seatbelt
        (ANTHROPIC + "/", None, None, "true"),
        ("https://llm-proxy.corp.example", None, None, "None"),  # may not forward its blocks
        (None, "https://llm-proxy.corp.example", None, "None"),  # chained from the environment
        (None, None, "false", "false"),  # the user's own choice stands
    ],
)
def test_claude_keeps_mcp_tool_search_when_the_recorder_forwards_to_anthropic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    upstream: str | None,
    base_url: str | None,
    own: str | None,
    expected: str,
) -> None:
    """Behind any base URL but Anthropic's, Claude Code turns MCP tool search off and puts
    every MCP tool's schema in the context up front, unless ENABLE_TOOL_SEARCH says otherwise.
    The recorder forwards tool search's `tool_reference` blocks unchanged."""
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    for name, value in (("ANTHROPIC_BASE_URL", base_url), ("ENABLE_TOOL_SEARCH", own)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    config = tmp_path / "config.toml"
    config.write_text(f'[upstreams]\nanthropic = "{upstream}"\n' if upstream else "")
    claude = fake_claude(tmp_path)
    assert run_cli("claude", ["-c", TOOL_SEARCH_PROBE], config=config, exe=claude) == 0
    assert capfd.readouterr().out.strip() == expected


def test_only_claude_is_given_tool_search() -> None:
    assert local_defaults("claude", {}, ANTHROPIC, {}) == {"ENABLE_TOOL_SEARCH": "true"}
    assert local_defaults("codex", {}, ANTHROPIC, {}) == {}
    assert local_defaults("gemini", {}, ANTHROPIC, {}) == {}


def test_the_local_port_refuses_anyone_without_the_runs_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    """Another process on this machine can reach the port, but not write to the ledger: the
    run's key is in the base URL's path, which it does not have."""
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    bare = 'BASE = os.environ["ANTHROPIC_BASE_URL"].split("/_seatbelt/")[0]'
    intruder = CLAUDE.replace('BASE = os.environ["ANTHROPIC_BASE_URL"]', bare)
    intruder = intruder.replace(
        'print(json.load(urllib.request.urlopen(req))["content"][0]["text"])\nsys.exit(7)',
        "try:\n    urllib.request.urlopen(req)\nexcept urllib.error.HTTPError as e:\n"
        "    sys.exit(3 if e.code == 401 else 4)",
    )
    assert intruder != CLAUDE
    code = run_cli(
        "claude",
        ["-c", "import urllib.error\n" + intruder],
        config=_config(tmp_path, provider),
        exe=fake_claude(tmp_path),
    )
    assert code == 3 and provider.seen == []  # 401, and nothing reached the provider
    assert list((tmp_path / "home" / "runs").glob("*.jsonl")) == []


def test_data_dir_follows_each_platform(tmp_path: Path) -> None:
    home = Path.home()
    assert data_dir({}, "linux") == home / ".local" / "share" / "seatbelt"
    assert data_dir({"XDG_DATA_HOME": "/x"}, "linux") == Path("/x/seatbelt")
    assert data_dir({}, "darwin") == home / "Library" / "Application Support" / "seatbelt"
    assert data_dir({"LOCALAPPDATA": "C:/u"}, "win32") == Path("C:/u/seatbelt")
    assert data_dir({"SEATBELT_HOME": str(tmp_path)}, "linux") == tmp_path


def test_with_no_config_a_run_is_recorded_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from seatbelt.gateway import launcher

    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(launcher, "LEGACY_CONFIG", tmp_path / "gateway.toml")
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    probe = (
        "import os; url = os.environ['ANTHROPIC_BASE_URL']; "
        "raise SystemExit(0 if url.startswith('http://127.0.0.1:') else 1)"
    )
    assert run_cli("claude", ["-c", probe], exe=fake_claude(tmp_path)) == 0
    with pytest.raises(ValueError, match="no preset to record aider locally"):
        run_cli("aider", [], exe=sys.executable)


def test_codex_keeps_its_own_login_and_its_key_off_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Any user on the machine can read a process's arguments, so the run's key goes to
    Codex in its environment, which Codex sends as a header to the provider alone."""
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "print(repr([sys.argv[1:], os.environ['SEATBELT_RUN_KEY']]))\n"
    )
    fake.chmod(0o755)
    empty = tmp_path / "config.toml"
    empty.write_text("")
    run_cli("codex", ["exec", "hi"], config=empty, exe=str(fake))
    argv, key = ast.literal_eval(capfd.readouterr().out.strip())
    assert argv[:3] == ["-c", 'model_provider="seatbelt"', "-c"] and argv[-2:] == ["exec", "hi"]
    assert key.startswith("sbk_") and not any(key in a for a in argv)
    provider = tomllib.loads(f"x = {argv[3].partition('=')[2]}")["x"]
    assert provider["base_url"].startswith("http://127.0.0.1:")
    assert provider["base_url"].endswith("/v1")
    assert provider["requires_openai_auth"] is True and "env_key" not in provider
    assert provider["supports_websockets"] is False
    assert provider["http_headers"]["X-Seatbelt-Run"].startswith("codex-")
    assert provider["env_http_headers"] == {"X-Seatbelt-Key": "SEATBELT_RUN_KEY"}


def test_a_base_url_the_cli_already_has_is_where_the_recorder_forwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    """Your own LLM gateway, say, with its token in ANTHROPIC_AUTH_TOKEN: recorded, then on
    to it, not to api.anthropic.com."""
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", provider.url + "/")
    empty = tmp_path / "config.toml"
    empty.write_text("")
    assert run_cli("claude", ["-c", CLAUDE], config=empty, exe=fake_claude(tmp_path)) == 7
    (sent,) = provider.seen
    assert sent["path"] == "/v1/messages?beta=true"


def _open_ledger(ledgers: Path, run: str) -> Path:
    """A ledger a run opened and never closed, as a killed run leaves it."""
    from seatbelt.ledger.events import Actor, ActorType
    from seatbelt.ledger.store import Ledger

    path = ledgers / f"me-{run}.jsonl"
    ledger = Ledger(path, f"me-{run}")
    ledger.append(Kind.RUN_START, Actor(type=ActorType.AGENT, id="gw"), {"run.name": run})
    ledger.append(Kind.USER_MESSAGE, Actor(type=ActorType.USER, id="u"), {"text": "hi"})
    return path


def test_a_killed_runs_ledger_is_closed_by_the_next_run_and_a_live_ones_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import signal
    import subprocess
    import time

    from seatbelt.gateway.local import RUNNING, close_dead_runs, local_signer

    home = tmp_path / "home"
    ledgers, keys = home / "runs", home / "keys"
    monkeypatch.setenv("SEATBELT_HOME", str(home))
    ledgers.mkdir(parents=True)
    # a live run in another process: holds its lock, then waits to be killed
    holder = (
        "import fcntl, sys, time; f = open(sys.argv[1], 'wb'); "
        "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(60)"
    )
    (ledgers / RUNNING).mkdir()
    lock = ledgers / RUNNING / "claude-live.lock"
    child = subprocess.Popen(  # noqa: S603 - this interpreter
        [sys.executable, "-c", holder, str(lock)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        live = _open_ledger(ledgers, "claude-live")
        dead = _open_ledger(ledgers, "claude-dead")  # no lock at all: its run is gone
        signer = local_signer(keys)
        assert close_dead_runs(ledgers, signer) == [dead]
        assert [e.kind for e in read_events(live)][-1] is Kind.USER_MESSAGE  # left alone
        end = list(read_events(dead))[-1]
        assert end.kind is Kind.RUN_END and "killed" in end.attrs["run.error"]
        assert sidecar(dead).exists()
    finally:
        os.kill(child.pid, signal.SIGKILL)
        child.wait()
    time.sleep(0.1)
    assert close_dead_runs(ledgers, signer) == [live]  # its process is dead now
    assert not lock.exists()  # a dead run's lock file is removed


def test_a_run_closes_what_a_killed_run_left_open_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SEATBELT_HOME", str(home))
    (home / "runs").mkdir(parents=True)
    dead = _open_ledger(home / "runs", "claude-dead")
    empty = tmp_path / "config.toml"
    empty.write_text("")
    assert run_cli("claude", ["-c", "pass"], config=empty, exe=fake_claude(tmp_path)) == 0
    assert list(read_events(dead))[-1].kind is Kind.RUN_END
    assert page_path(dead).exists()  # closed and signed by this run: it gets its page too
    assert list((home / "runs" / ".running").iterdir()) == []  # this run's lock is gone too


def test_a_user_with_no_login_name_is_named_by_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    import getpass
    import os

    from seatbelt.gateway import local

    def nobody() -> str:
        raise OSError("no username")

    monkeypatch.setattr(getpass, "getuser", nobody)
    assert local.login_name() == f"uid-{os.getuid()}"
