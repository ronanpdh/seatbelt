"""`seatbelt run <cli>`: preset the environment so an existing CLI talks to the gateway.

Stdlib only: this runs on employee machines, which need no server dependencies."""

from __future__ import annotations

import os
import secrets
import signal
import stat
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "seatbelt" / "gateway.toml"

PRESETS: dict[str, dict[str, str]] = {  # cli -> env template
    "claude": {
        "ANTHROPIC_BASE_URL": "{url}",
        "ANTHROPIC_AUTH_TOKEN": "{key}",  # sent as Authorization: Bearer
        "ANTHROPIC_CUSTOM_HEADERS": "X-Seatbelt-Run: {run}",
    },
    "codex": {"OPENAI_BASE_URL": "{url}/v1", "OPENAI_API_KEY": "{key}"},
}
# real provider credentials never reach the child, so it cannot bypass the gateway by accident
_PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")

EndRun = Callable[[str, str, str], None]  # (gateway url, key, run name)


def load_client_config(path: Path = DEFAULT_CONFIG) -> tuple[str, str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return str(data["url"]).rstrip("/"), str(data["key"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: {exc}; expected url = ... and key = ...") from exc


def readable_by_others(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def environment(cli: str, url: str, key: str, run: str, base: Mapping[str, str]) -> dict[str, str]:
    if cli not in PRESETS:
        raise ValueError(f"no preset for {cli}; known: {', '.join(PRESETS)}")
    env = {k: v for k, v in base.items() if k not in _PROVIDER_KEYS}
    for name, template in PRESETS[cli].items():
        value = template.format(url=url, key=key, run=run)
        if name == "ANTHROPIC_CUSTOM_HEADERS" and base.get(name):
            value = f"{base[name]}\n{value}"  # keep the user's own headers
        env[name] = value
    return env


def end_run(url: str, key: str, run: str) -> None:
    request = urllib.request.Request(  # noqa: S310 - the org's configured gateway URL
        f"{url}/seatbelt/runs/{run}/end",
        method="POST",
        headers={"authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310
            pass
    except (urllib.error.URLError, OSError):
        pass  # 404 (nothing was sent) or unreachable: the idle sweeper closes it


def run_cli(
    cli: str,
    args: list[str],
    config: Path = DEFAULT_CONFIG,
    exe: str | None = None,
    end: EndRun = end_run,
) -> int:
    url, key = load_client_config(config)
    if readable_by_others(config):
        print(f"warning: {config} holds your gateway key; chmod 600 it", file=sys.stderr)
    run = f"{cli}-{secrets.token_hex(4)}"
    env = environment(cli, url, key, run, os.environ)
    try:
        child = subprocess.Popen([exe or cli, *args], env=env)  # noqa: S603 - the user's CLI
        # Ctrl-C belongs to the child (Claude Code cancels a response with it); the terminal
        # sends it to both. Ignore it only after the spawn: an ignored signal is inherited.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return child.wait()
        finally:
            signal.signal(signal.SIGINT, previous)
    finally:
        end(url, key, run)
