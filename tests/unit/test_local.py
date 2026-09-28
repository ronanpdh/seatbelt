"""`seatbelt run` recording on this machine: the gateway starts inside the run, the CLI keeps
its own credentials, and the ledger lands in the local data folder, signed."""

import ast
import json
import sys
import threading
import tomllib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import PUB_FILE
from seatbelt.gateway.launcher import data_dir, run_cli
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events

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
    code = run_cli("claude", ["-c", CLAUDE], config=_config(tmp_path, provider), exe=sys.executable)
    out, err = capfd.readouterr()
    assert code == 7 and out.strip() == "hi"  # the CLI's exit code and output
    (sent,) = provider.seen
    assert sent["path"] == "/v1/messages?beta=true"
    headers = {k.lower(): v for k, v in sent["headers"].items()}
    assert headers["x-api-key"] == "sk-ant-USERS-OWN"  # passed through, not replaced
    assert "x-seatbelt-key" not in headers and "x-seatbelt-run" not in headers
    (ledger,) = (tmp_path / "home" / "runs").glob("*.jsonl")
    assert f"seatbelt: recorded {ledger}" in err
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
        exe=sys.executable,
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
    assert run_cli("claude", ["-c", probe], exe=sys.executable) == 0
    with pytest.raises(ValueError, match="no preset to record aider locally"):
        run_cli("aider", [], exe=sys.executable)


def test_codex_keeps_its_own_login_and_names_the_runs_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\nimport sys; print(repr(sys.argv[1:]))\n")
    fake.chmod(0o755)
    empty = tmp_path / "config.toml"
    empty.write_text("")
    run_cli("codex", ["exec", "hi"], config=empty, exe=str(fake))
    argv = ast.literal_eval(capfd.readouterr().out.strip())
    assert argv[:3] == ["-c", 'model_provider="seatbelt"', "-c"] and argv[-2:] == ["exec", "hi"]
    provider = tomllib.loads(f"x = {argv[3].partition('=')[2]}")["x"]
    origin, _, path = provider["base_url"].partition("/_seatbelt/")
    assert origin.startswith("http://127.0.0.1:")
    key, run, v1 = path.split("/")
    assert key.startswith("sbk_") and run.startswith("codex-") and v1 == "v1"
    assert provider["requires_openai_auth"] is True and "env_key" not in provider
    assert provider["supports_websockets"] is False and "http_headers" not in provider
