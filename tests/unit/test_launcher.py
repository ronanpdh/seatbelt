import ast
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from seatbelt.cli import app
from seatbelt.gateway.launcher import (
    CODEX_KEY_ENV,
    PRESETS,
    arguments,
    environment,
    load_client_config,
    run_cli,
)


def _config(tmp_path: Path, mode: int = 0o600) -> Path:
    p = tmp_path / "gateway.toml"
    p.write_text('url = "https://gw.corp/"\nkey = "sbk_abc"\n')
    p.chmod(mode)
    return p


def test_client_config_is_read_from_toml(tmp_path: Path) -> None:
    assert load_client_config(_config(tmp_path)) == ("https://gw.corp", "sbk_abc")
    with pytest.raises(ValueError):
        load_client_config(tmp_path / "missing.toml")
    (tmp_path / "bad.toml").write_text('url = "x"\n')
    with pytest.raises(ValueError, match="key"):
        load_client_config(tmp_path / "bad.toml")


def test_claude_preset_sets_base_url_token_and_run_header() -> None:
    base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-real", "OPENAI_API_KEY": "sk-real"}
    env = environment("claude", "https://gw.corp", "sbk_abc", "run-1", base=base)
    assert env["ANTHROPIC_BASE_URL"] == "https://gw.corp"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sbk_abc"  # noqa: S105 - a fake test key
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Seatbelt-Run: run-1"
    assert env["PATH"] == "/bin"


def test_claude_preset_keeps_the_users_own_custom_headers() -> None:
    env = environment("claude", "u", "k", "r", base={"ANTHROPIC_CUSTOM_HEADERS": "X-Team: a"})
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Team: a\nX-Seatbelt-Run: r"


def test_codex_preset_gets_the_key_from_the_environment_only() -> None:
    base = {"PATH": "/bin", "OPENAI_API_KEY": "sk-real", "CODEX_API_KEY": "sk-codex"}
    env = environment("codex", "https://gw.corp", "sbk_abc", "run-1", base=base)
    assert env[CODEX_KEY_ENV] == "sbk_abc"
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env
    assert env["PATH"] == "/bin"


def _provider(args: list[str]) -> dict[str, Any]:  # the TOML value Codex parses
    assert args[:3] == ["-c", 'model_provider="seatbelt"', "-c"]
    key, _, value = args[3].partition("=")
    assert key == "model_providers.seatbelt"
    return tomllib.loads(f"x = {value}")["x"]


def test_codex_preset_defines_a_responses_provider_on_the_command_line() -> None:
    assert _provider(arguments("codex", "https://gw.corp", "codex-1a2b")) == {
        "name": "seatbelt",
        "base_url": "https://gw.corp/v1",
        "env_key": CODEX_KEY_ENV,
        "wire_api": "responses",
        "requires_openai_auth": False,
        "supports_websockets": False,
        "http_headers": {"X-Seatbelt-Run": "codex-1a2b"},
    }
    odd = 'https://gw.corp/a "quoted" \\ path/ü'  # still one valid TOML string
    assert _provider(arguments("codex", odd, "r"))["base_url"] == odd + "/v1"
    assert arguments("claude", "https://gw.corp", "r") == []


def test_run_cli_puts_the_codex_provider_before_the_users_arguments(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\nimport sys; print(repr(sys.argv[1:]))\n")
    fake.chmod(0o755)

    def keep_open(url: str, key: str, run: str) -> None:
        pass

    run_cli("codex", ["exec", "hi"], config=_config(tmp_path), exe=str(fake), end=keep_open)
    argv = ast.literal_eval(capfd.readouterr().out.strip())
    assert argv[-2:] == ["exec", "hi"] and len(argv) == 6
    assert _provider(argv[:4])["http_headers"]["X-Seatbelt-Run"].startswith("codex-")


def test_unknown_cli_is_refused() -> None:
    with pytest.raises(ValueError):
        environment("vim", "u", "k", "r", base={})
    assert set(PRESETS) >= {"claude", "codex"}


PROBE = (
    "import os, signal; "
    "print(os.environ['ANTHROPIC_BASE_URL'], os.environ['ANTHROPIC_CUSTOM_HEADERS'], "
    "signal.getsignal(signal.SIGINT) is signal.SIG_IGN); raise SystemExit(3)"
)


def test_run_cli_spawns_with_the_preset_then_ends_the_run(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    ended: list[tuple[str, str, str]] = []
    code = run_cli(
        "claude",
        ["-c", PROBE],
        config=_config(tmp_path),
        exe=sys.executable,
        end=lambda url, key, run: ended.append((url, key, run)),
    )
    out = capfd.readouterr().out.split()
    assert code == 3  # the child's exit code
    assert out[0] == "https://gw.corp" and out[1] == "X-Seatbelt-Run:"
    assert out[3] == "False"  # the child still gets Ctrl-C
    ((url, key, run),) = ended
    assert (url, key) == ("https://gw.corp", "sbk_abc") and run == out[2]
    assert run.startswith("claude-")


def test_run_cli_warns_about_a_readable_key_file(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    run_cli(
        "claude",
        ["-c", "pass"],
        config=_config(tmp_path, 0o644),
        exe=sys.executable,
        end=lambda url, key, run: None,
    )
    assert "chmod 600" in capfd.readouterr().err


def test_cli_run_passes_arguments_and_exit_code(tmp_path: Path) -> None:
    env_bin = "/usr/bin/env" if os.path.exists("/usr/bin/env") else "env"
    r = CliRunner().invoke(
        app,
        ["run", "claude", "--config", str(_config(tmp_path)), "--exe", env_bin, "--", "true"],
    )
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(app, ["run", "vim", "--config", str(_config(tmp_path))])
    assert r.exit_code == 1 and "no preset" in r.output


def test_end_run_posts_to_the_gateway_and_tolerates_it_being_down() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from seatbelt.gateway.launcher import end_run

    seen: list[tuple[str, str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append((self.command, self.path, self.headers.get("authorization")))
            self.send_response(404)  # e.g. the run never sent a request
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    end_run(url, "sbk_abc", "claude-1234")
    server.server_close()
    assert seen == [("POST", "/seatbelt/runs/claude-1234/end", "Bearer sbk_abc")]
    end_run(url, "sbk_abc", "claude-1234")  # nothing listening now: no exception
