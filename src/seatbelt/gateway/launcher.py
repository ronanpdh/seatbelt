"""`seatbelt run <cli>`: preset the environment (and, for Codex, a model provider on its
command line) so an existing CLI is recorded.

With no gateway in the client config, the run is recorded on this machine: a gateway starts
inside this process for the run (`seatbelt.gateway.local`) and the CLI keeps its own
credentials. With `gateway = <url>` and `key`, it goes through the org's gateway instead.

Stdlib only at import: the local recorder's server code is imported when a run needs it."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from seatbelt.gateway import badge, claude_code
from seatbelt.gateway.badge import say
from seatbelt.terminal import printable

CONFIG_DIR = Path.home() / ".config" / "seatbelt"
DEFAULT_CONFIG = CONFIG_DIR / "config.toml"
LEGACY_CONFIG = CONFIG_DIR / "gateway.toml"  # 0.2.0: url and key only

CODEX_KEY_ENV = "SEATBELT_GATEWAY_KEY"  # the env var the codex preset's provider reads
RUN_KEY_ENV = "SEATBELT_RUN_KEY"  # the local run's key, which Codex sends as a header
PRESETS: dict[str, dict[str, str]] = {  # cli -> env template
    "claude": {
        "ANTHROPIC_BASE_URL": "{url}",
        "ANTHROPIC_AUTH_TOKEN": "{key}",  # sent as Authorization: Bearer
        "ANTHROPIC_CUSTOM_HEADERS": "X-Seatbelt-Run: {run}",
    },
    "codex": {CODEX_KEY_ENV: "{key}"},  # the rest is on the command line, see `arguments`
    # read in Gemini CLI's API-key mode only; see `gemini_warnings`. GEMINI_CLI_CUSTOM_HEADERS
    # is undocumented: see docs/plans/2026-09-28-gemini-format.md
    "gemini": {
        "GEMINI_API_KEY": "{key}",
        "GOOGLE_GEMINI_BASE_URL": "{url}",
        "GEMINI_CLI_CUSTOM_HEADERS": "X-Seatbelt-Run: {run}",
        # set, not only stripped: Gemini CLI loads a .env file's value for any variable the
        # environment lacks, and these would switch it away from the gateway
        "GOOGLE_API_KEY": "",
        "GOOGLE_GENAI_USE_VERTEXAI": "false",
        "GOOGLE_GENAI_USE_GCA": "false",
    },
}
# local mode: the CLI keeps its own credentials, and its base URL carries the run's key and
# name in its path (`{url}` here is the recorder's URL with that path; see `local_url`), not
# custom headers, which some CLIs also send to other hosts
LOCAL_PRESETS: dict[str, dict[str, str]] = {
    "claude": {"ANTHROPIC_BASE_URL": "{url}"},
    # the rest is on the command line (see `arguments`); the key stays off it, where any
    # user on the machine could read it, and in the environment, which only this user can
    "codex": {RUN_KEY_ENV: "{key}"},
    "gemini": {
        "GOOGLE_GEMINI_BASE_URL": "{url}",  # signed in with an API key
        "CODE_ASSIST_ENDPOINT": "{url}",  # signed in with Google
    },
}


# a base URL the CLI already has, which the local recorder forwards to in its place: your own
# LLM gateway, say. Its credentials, e.g. ANTHROPIC_AUTH_TOKEN, go on with it
_CHAINED = {
    "ANTHROPIC_BASE_URL": "anthropic",
    "GOOGLE_GEMINI_BASE_URL": "gemini",
    "CODE_ASSIST_ENDPOINT": "codeassist",
}


def chained_upstreams(env: Mapping[str, str]) -> dict[str, str]:
    """Upstreams from base URLs already in the environment (not another run's recorder)."""
    return {
        name: env[var].rstrip("/")
        for var, name in _CHAINED.items()
        if env.get(var) and "/_seatbelt/" not in env[var]
    }


# Claude Code turns MCP tool search off behind any ANTHROPIC_BASE_URL but Anthropic's, since a
# proxy may not forward the `tool_reference` blocks it uses, and then puts every MCP tool's
# schema in the context up front. The local recorder forwards them unchanged: see
# `local_defaults` and docs/local-recording.md
TOOL_SEARCH_ENV = "ENABLE_TOOL_SEARCH"


def local_defaults(
    cli: str, upstreams: Mapping[str, str], anthropic: str, base: Mapping[str, str]
) -> dict[str, str]:
    """What a local run adds to the CLI's environment so that it works as it does without
    seatbelt: Claude Code's MCP tool search, when the recorder forwards to `anthropic`
    (Anthropic's API) and the user has not set it. A proxy of the user's own may not forward
    tool search's blocks, so behind one Claude Code keeps its own default (off)."""
    if cli != "claude" or TOOL_SEARCH_ENV in base:
        return {}
    if upstreams.get("anthropic", anthropic).rstrip("/") != anthropic:
        return {}
    return {TOOL_SEARCH_ENV: "true"}


def local_url(recorder: str, key: str, run: str) -> str:
    """The base URL a CLI recording locally is given: the recorder's, with the run's key and
    name in the path, which the recorder takes off (`seatbelt.gateway.app.PATH_PREFIX`)."""
    return f"{recorder}/_seatbelt/{key}/{run}"


NOT_YET: dict[str, str] = {}  # clients a preset would launch but the gateway cannot record yet
# real provider credentials never reach the child, so it cannot bypass the gateway by accident
_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    # these switch Gemini CLI to Vertex AI or a Google login, which do not use the base URL
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_GCA",
)
# these switch Claude Code to Bedrock or Vertex AI, which do not use the base URL, or are a
# subscription login it could use itself: removed, not set to a value it may read as false
_AROUND_GATEWAY = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
# seatbelt's own secrets, already read by this process: never the CLI's or its tools'
_SIGNING_KEY_ENV = "SEATBELT_SIGNING_KEY"
_SINK_ENV_PREFIX = "SEATBELT_SINK_"
_HEADER_SEPARATOR = {"ANTHROPIC_CUSTOM_HEADERS": "\n", "GEMINI_CLI_CUSTOM_HEADERS": ", "}

# (gateway url, key, run name) -> the gateway's HTTP status, None if it was not reached
EndRun = Callable[[str, str, str], int | None]
# (gateway url, key) -> the gateway's `GET /seatbelt/policy`, None from an older gateway
Preflight = Callable[[str, str], dict[str, Any] | None]


def data_dir(env: Mapping[str, str] = os.environ, platform: str = sys.platform) -> Path:
    """Where local runs live: `$SEATBELT_HOME`, else the platform's per-user data folder:
    `runs/` for the ledgers and `keys/` for this machine's signing key."""
    if env.get("SEATBELT_HOME"):
        return Path(env["SEATBELT_HOME"]).expanduser()
    home = Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / "seatbelt"
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "seatbelt"
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "seatbelt"


@dataclass(frozen=True)
class ClientConfig:
    """`~/.config/seatbelt/config.toml`. Every key is optional::

    gateway = "https://gw.corp.example"  # record through the org's gateway, with
    key = "sbk_..."                      # your issued key; unset: record locally
    ledgers = "~/seatbelt/runs"          # local: where ledgers go
    [upstreams]                          # local: providers' URLs, e.g. a proxy
    anthropic = "https://llm-proxy.corp.example"
    [sink]                               # local: also ship ledgers to object storage
    url = "https://fsn1.your-objectstorage.com"
    bucket = "ledgers"
    region = "fsn1"
    """

    gateway: str | None = None
    key: str | None = None
    ledgers: Path | None = None
    upstreams: dict[str, str] = field(default_factory=dict[str, str])
    sink: dict[str, Any] | None = None
    path: Path | None = None  # the file it came from, if any


_CLIENT_KEYS = {"gateway", "url", "key", "ledgers", "upstreams", "sink"}


def load_client_config(path: Path | None = None) -> ClientConfig:
    """The config at `path`; with none given, `config.toml`, else 0.2.0's `gateway.toml`
    (`url` is read as `gateway`), else none: record locally."""
    ignored: Path | None = None
    if path is None:
        path = next((p for p in (DEFAULT_CONFIG, LEGACY_CONFIG) if p.exists()), None)
        if path is None:
            return ClientConfig()
        if path == DEFAULT_CONFIG and LEGACY_CONFIG.exists():
            ignored = LEGACY_CONFIG
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    unknown = sorted(set(data) - _CLIENT_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown keys {', '.join(unknown)}")
    gateway = data.get("gateway") or data.get("url")
    key = data.get("key")
    if gateway is not None and not (isinstance(gateway, str) and isinstance(key, str) and key):
        raise ValueError(f'{path}: a gateway needs your key: key = "sbk_..."')
    upstreams = data.get("upstreams", {})
    if not isinstance(upstreams, dict) or not all(
        isinstance(v, str) for v in cast(dict[str, Any], upstreams).values()
    ):
        raise ValueError(f"{path}: [upstreams] maps a provider to its URL")
    sink = data.get("sink")
    if sink is not None and not isinstance(sink, dict):
        raise ValueError(f"{path}: [sink] is a table")
    ledgers = data.get("ledgers")
    if ignored is not None and gateway is None:
        say(
            f"warning: {ignored} is ignored, since {path} is read instead and names no "
            f'gateway: runs are recorded on this machine. Put gateway = "..." and key in {path}'
        )
    return ClientConfig(
        gateway=gateway.rstrip("/") if isinstance(gateway, str) else None,
        key=key if isinstance(key, str) else None,
        ledgers=path.parent / Path(ledgers).expanduser() if isinstance(ledgers, str) else None,
        upstreams=cast(dict[str, str], upstreams),
        sink=cast(dict[str, Any], sink) if sink is not None else None,
        path=path,
    )


def readable_by_others(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def own_secrets(cfg: ClientConfig) -> list[str]:
    """The environment variables the config names for seatbelt's own secrets: the sink's
    credentials, which may be renamed from their defaults."""
    sink = cfg.sink or {}
    return [v for k in ("access_key_env", "secret_key_env") if isinstance(v := sink.get(k), str)]


def _seatbelts(name: str, drop: Collection[str]) -> bool:
    return name == _SIGNING_KEY_ENV or name.startswith(_SINK_ENV_PREFIX) or name in drop


def environment(
    cli: str,
    url: str,
    key: str,
    run: str,
    base: Mapping[str, str],
    local: bool = False,
    drop: Collection[str] = (),
) -> dict[str, str]:
    """The CLI's environment. Through a gateway, its own provider credentials are removed,
    so it cannot go around the gateway by accident; locally they are what it signs in with.
    Seatbelt's own secrets (a signing key, the sink's credentials, and the names in `drop`)
    are removed either way."""
    presets = LOCAL_PRESETS if local else PRESETS
    if cli in NOT_YET:
        raise ValueError(f"{cli} is not supported yet: {NOT_YET[cli]}")
    if cli not in presets:
        where = "locally" if local else "through a gateway"
        raise ValueError(f"no preset to record {cli} {where}; known: {', '.join(presets)}")
    around = () if local else (*_PROVIDER_KEYS, *_AROUND_GATEWAY)
    env = {k: v for k, v in base.items() if k not in around and not _seatbelts(k, drop)}
    for name, template in presets[cli].items():
        value = template.format(url=url, key=key, run=run)
        if name in _HEADER_SEPARATOR and base.get(name):
            value = f"{base[name]}{_HEADER_SEPARATOR[name]}{value}"  # keep the user's own
        env[name] = value
    return env


def _toml(value: str) -> str:
    """A TOML basic string: JSON's string escapes are TOML's too."""
    return json.dumps(value, ensure_ascii=False)


def arguments(cli: str, url: str, run: str, local: bool = False) -> list[str]:
    """Arguments a preset puts before the user's own. Codex takes its provider from `-c`
    overrides (TOML values; global, so they also apply before a subcommand): the built-in
    `openai` provider cannot carry the run header, and `model_providers.openai` is reserved.
    The custom provider speaks Responses over HTTP (no WebSocket), so the gateway sees every
    turn, and reads the gateway key from the environment. See
    docs/plans/2026-09-28-responses-format.md for the sources.

    Recording locally (`url` the local recorder's), the provider signs in with Codex's own
    stored login instead (`requires_openai_auth`: an API key, or ChatGPT, whose requests the
    local gateway sends on to ChatGPT's backend), and sends the run's key from the
    environment (`env_http_headers`) to the provider alone."""
    if cli != "codex":
        return []
    if local:
        rest = (
            "requires_openai_auth=true,"
            f'http_headers={{"X-Seatbelt-Run"={_toml(run)}}},'
            f'env_http_headers={{"X-Seatbelt-Key"={_toml(RUN_KEY_ENV)}}},'
        )
    else:
        rest = (
            f"env_key={_toml(CODEX_KEY_ENV)},requires_openai_auth=false,"
            f'http_headers={{"X-Seatbelt-Run"={_toml(run)}}},'
        )
    provider = (
        '{name="seatbelt",'
        f"base_url={_toml(url + '/v1')},"
        f"{rest}"
        'wire_api="responses",'
        "supports_websockets=false}"
    )
    return ["-c", 'model_provider="seatbelt"', "-c", f"model_providers.seatbelt={provider}"]


def _settings(path: Path) -> dict[str, Any] | None:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None  # unreadable, or JSON with comments, which Gemini CLI accepts: no guess
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


def _setting(settings: dict[str, Any], *keys: str) -> Any:
    value: Any = settings
    for key in keys:
        value = cast(dict[str, Any], value).get(key) if isinstance(value, dict) else None
    return value


def gemini_system_settings(env: Mapping[str, str], platform: str = sys.platform) -> Path:
    """Where Gemini CLI reads its system settings, which override every other file."""
    if env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH"):
        return Path(env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
    if platform == "darwin":
        return Path("/Library/Application Support/GeminiCli/settings.json")
    if platform == "win32":
        return Path("C:\\ProgramData\\gemini-cli\\settings.json")
    return Path("/etc/gemini-cli/settings.json")


# Gemini CLI sign-ins each recording mode sees: an org gateway holds an API key; locally,
# Google sign-ins go through CODE_ASSIST_ENDPOINT too (Vertex AI uses neither)
_GEMINI_RECORDED = {
    False: frozenset({"gemini-api-key"}),
    True: frozenset({"gemini-api-key", "oauth-personal", "compute-default-credentials"}),
}


def gemini_warnings(
    home: Path, cwd: Path, system: Path, system_defaults: Path, local: bool = False
) -> list[str]:
    """What in Gemini CLI's settings would take its traffic around the gateway. Gemini CLI
    uses the base URL only in its API-key sign-in, chosen by `security.auth.selectedType`:
    with none set, a base URL in the environment selects a mode it refuses. Its settings merge
    system defaults, the user's, the workspace's (only in a folder it trusts, which is not
    known here, so both ways are checked) and the system's, last winning."""
    user = home / ".gemini" / "settings.json"
    files = [system_defaults, user, cwd / ".gemini" / "settings.json", system]
    loaded = [_settings(f) for f in files]
    if any(s is None for s in loaded):
        return []
    defaults, mine, workspace, overrides = (cast(dict[str, Any], s) for s in loaded)

    def values(*keys: str) -> set[Any]:
        """The value in effect, with the workspace trusted and without."""
        out: set[Any] = set()
        for layers in ([defaults, mine, workspace, overrides], [defaults, mine, overrides]):
            found = [v for s in layers if (v := _setting(s, *keys)) is not None]
            value = found[-1] if found else None
            out.add(value if isinstance(value, str | bool) else None)
        return out

    warnings: list[str] = []
    selected = values("security", "auth", "selectedType")
    recorded = _GEMINI_RECORDED[local]
    if not selected <= recorded:
        now = " or ".join(sorted(str(v or "not set") for v in selected))
        choices = " or ".join(f'"{t}"' for t in sorted(recorded))
        warnings.append(
            f"Gemini CLI's sign-in is {now}, which is not recorded. Set "
            f'"security": {{"auth": {{"selectedType": {choices}}}}} in {user}'
        )
    if values("privacy", "usageStatisticsEnabled") != {False}:
        warnings.append(
            "Gemini CLI may send usage statistics to Google directly, unrecorded. "
            f'To stop it, set "privacy": {{"usageStatisticsEnabled": false}} in {user}'
        )
    return warnings


_CLAUDE_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


def claude_warnings(config_dir: Path, cwd: Path) -> list[str]:
    """What in Claude Code's settings would take its traffic around the gateway: an `env`
    block, which Claude Code applies over the environment it is given, switching it to
    Bedrock or Vertex AI, neither of which uses the base URL."""
    warnings: list[str] = []
    project = cwd / ".claude"
    for path in (
        config_dir / "settings.json",
        project / "settings.json",
        project / "settings.local.json",
    ):
        env = _setting(_settings(path) or {}, "env")
        if not isinstance(env, dict):
            continue
        values = cast(dict[str, Any], env)
        off = ("", "0", "false")  # which values it reads as false is not known here: a guess
        found = [n for n in _CLAUDE_SWITCHES if str(values.get(n, "")).lower() not in off]
        if found:
            warnings.append(
                f"{path} sets {', '.join(found)}, which sends Claude Code to Bedrock or Vertex "
                "AI directly, unrecorded. Remove it from that file's env block"
            )
    return warnings


def end_run(url: str, key: str, run: str) -> int | None:
    """End the run at the gateway: 204 if it recorded any of it, 404 if none, None if the
    gateway was not reached (its idle sweeper then closes the run)."""
    request = urllib.request.Request(  # noqa: S310 - the org's configured gateway URL
        f"{url}/seatbelt/runs/{run}/end",
        method="POST",
        headers={"authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return cast(int, response.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def fetch_policy(url: str, key: str) -> dict[str, Any] | None:
    """The gateway's `GET /seatbelt/policy`, before the CLI starts: None from a gateway too
    old to have it (404), which the run goes on with as before. A ValueError when the gateway
    cannot be reached or refuses the key, so a run that could record nothing never starts."""
    request = urllib.request.Request(  # noqa: S310 - the org's configured gateway URL
        f"{url}/seatbelt/policy", headers={"authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            data: Any = json.loads(response.read())  # the gateway's JSON
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return None
        if exc.code in (401, 403):
            raise ValueError(
                f"the gateway at {url} refused your key ({exc.code}): it may have been revoked "
                "or reissued; ask your admin for a new one"
            ) from exc
        raise ValueError(f"the gateway at {url} answered {exc.code}; try again shortly") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(f"cannot reach the gateway at {url}: {reason}") from exc
    except ValueError as exc:
        raise ValueError(f"the gateway at {url} did not answer with its policy") from exc
    if not isinstance(data, dict) or not isinstance(cast(dict[str, Any], data).get("policy"), dict):
        raise ValueError(f"the gateway at {url} did not answer with its policy")
    return cast(dict[str, Any], data)


def _names(value: Any) -> list[str]:
    return [str(v) for v in cast(list[Any], value)] if isinstance(value, list) else []


def denied_tools(answer: dict[str, Any] | None) -> list[str]:
    """The tools a `GET /seatbelt/policy` answer denies."""
    return _names(cast(dict[str, Any], (answer or {}).get("policy") or {}).get("tools_denied"))


def under_policy(answer: dict[str, Any] | None) -> bool:
    """Whether a `GET /seatbelt/policy` answer sets any rule."""
    policy = cast(dict[str, Any], (answer or {}).get("policy") or {})
    return (
        isinstance(policy.get("models"), list)
        or bool(_names(policy.get("tools_denied")))
        or isinstance(policy.get("max_output_tokens"), int)
    )


def policy_lines(answer: dict[str, Any]) -> list[str]:
    """The org's policy, as a run says it before the CLI starts."""
    policy = cast(dict[str, Any], answer.get("policy") or {})
    lines: list[str] = []
    if denied := _names(policy.get("tools_denied")):
        lines.append(f"the org's policy denies {', '.join(denied)}")
    if isinstance(policy.get("models"), list):
        allowed = _names(policy["models"])
        lines.append(f"the org's policy allows the models {', '.join(allowed) or '(none)'}")
    if isinstance(cap := policy.get("max_output_tokens"), int):
        lines.append(f"the org's policy caps output at {cap} tokens")
    # names the gateway gave, shown safe for a terminal
    return [printable(line) for line in lines] or ["the org's policy sets no limits"]


def _gemini_warnings(local: bool) -> list[str]:
    home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
    system = gemini_system_settings(os.environ)
    defaults = Path(
        os.environ.get("GEMINI_CLI_SYSTEM_DEFAULTS_PATH") or system.parent / "system-defaults.json"
    )
    return [f"warning: {w}" for w in gemini_warnings(home, Path.cwd(), system, defaults, local)]


def _claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _claude_warnings() -> list[str]:
    return [f"warning: {w}" for w in claude_warnings(_claude_config_dir(), Path.cwd())]


def cli_arguments(
    cli: str, args: list[str], denied: Collection[str] = (), note: str = claude_code.RECORDING
) -> tuple[list[str], list[str]]:
    """(The user's arguments for `cli` with what seatbelt gives it, warnings to show). For
    Claude Code: a `--settings` with seatbelt's status line, which says `note` after the
    badge, and a hook that stops the `denied` tools."""
    if cli != "claude":
        return args, []
    cwd = Path.cwd()
    out, warnings = claude_code.launch_arguments(args, denied, _claude_config_dir(), cwd, note)
    return out, [f"warning: {w}" for w in warnings]


def _buckle_up(lines: list[str]) -> None:
    """Buckle the seatbelt, then say `lines` below it, as issue #46 drew it."""
    badge.buckle()
    for line in lines:
        say(line)


GRACE = 10.0  # seconds the CLI has to exit after seatbelt passes it SIGTERM or SIGHUP
_PASSED_ON = tuple(getattr(signal, n) for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n))


def _spawn(command: list[str], env: dict[str, str]) -> int:
    """Run the CLI; its exit status as a shell gives it: 128 + the signal's number if a signal
    killed it. SIGTERM and SIGHUP sent to seatbelt are passed on to the CLI, which is killed
    if it has not exited GRACE seconds later: seatbelt outlives it to close the run."""
    child = subprocess.Popen(command, env=env)  # noqa: S603 - the user's CLI
    # Ctrl-C belongs to the child (Claude Code cancels a response with it); the terminal
    # sends it to both. Ignore it only after the spawn: an ignored signal is inherited.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    killer: list[threading.Timer] = []

    def pass_on(signum: int, _frame: object) -> None:
        child.send_signal(signum)
        if not killer:
            killer.append(threading.Timer(GRACE, child.kill))
            killer[0].daemon = True
            killer[0].start()

    handlers = {s: signal.signal(s, pass_on) for s in _PASSED_ON}
    try:
        code = child.wait()
    finally:
        for s, handler in handlers.items():
            signal.signal(s, handler)
        signal.signal(signal.SIGINT, previous)
        for timer in killer:
            timer.cancel()
    return 128 - code if code < 0 else code


def run_cli(
    cli: str,
    args: list[str],
    config: Path | None = None,
    exe: str | None = None,
    end: EndRun = end_run,
    preflight: Preflight | None = None,
) -> int:
    """Launch `cli` recorded: through the configured gateway, else on this machine.
    `preflight` asks the gateway for its policy (default `fetch_policy`)."""
    # seatbelt's log lines (and, recording locally, its server's) carry the badge too
    with badge.said_logs("seatbelt", "uvicorn"):
        return _run(cli, args, config, exe, end, preflight or fetch_policy)


def _run(
    cli: str,
    args: list[str],
    config: Path | None,
    exe: str | None,
    end: EndRun,
    preflight: Preflight,
) -> int:
    cfg = load_client_config(config)
    if cfg.path is not None and cfg.key and readable_by_others(cfg.path):
        say(f"warning: {cfg.path} holds your gateway key; chmod 600 it")
    gateway = (cfg.gateway, cfg.key) if cfg.gateway and cfg.key else None
    warnings = _gemini_warnings(local=gateway is None) if cli == "gemini" else []
    run = f"{cli}-{secrets.token_hex(4)}"
    if gateway is None:
        return _run_local(cli, args, cfg, exe, run, warnings)
    url, key = gateway
    env = environment(cli, url, key, run, os.environ, drop=own_secrets(cfg))
    removed = [n for n in _AROUND_GATEWAY if os.environ.get(n)]
    if removed:
        warnings.append(
            f"warning: {', '.join(removed)} removed from {cli}'s environment: they would take "
            "it around the gateway, or give it a login of its own"
        )
    if cli == "claude":
        warnings += _claude_warnings()
    answer = preflight(url, key)  # before anything starts: a refused key stops here
    note = claude_code.UNDER_POLICY if under_policy(answer) else claude_code.RECORDING
    own, more = cli_arguments(cli, args, denied_tools(answer), note)
    command = [exe or cli, *arguments(cli, url, run), *own]
    policy = policy_lines(answer) if answer is not None else []
    _buckle_up([f"recording {cli} through the gateway at {url}", *policy, *warnings, *more])
    try:
        code = _spawn(command, env)
    finally:
        badge.unbuckle()
        status = end(url, key, run)
    if status == 204:
        say(f"recorded run {run} at {url}")
    elif status == 404:  # no open run by that name
        say(
            f"no open run {run} at {url}: {cli} sent no model requests, or the "
            "gateway already closed the run after it went idle"
        )
    else:
        say(
            f"could not end run {run} at {url} (status {status or 'none'}); the "
            "gateway closes it when idle, if it recorded anything"
        )
    return code


def _run_local(
    cli: str, args: list[str], cfg: ClientConfig, exe: str | None, run: str, warnings: list[str]
) -> int:
    environment(cli, "", "", run, {}, local=True)  # an unsupported CLI fails before any start
    where = f"no gateway in {cfg.path}" if cfg.path is not None else "no client config"
    try:
        from seatbelt.gateway.local import UPSTREAMS, local_recorder
    except ImportError as exc:  # a broken install: the server packages are dependencies
        raise ValueError(f"recording locally needs seatbelt's server packages: {exc}") from exc
    root = data_dir()
    ledgers = cfg.ledgers or root / "runs"
    upstreams = {**chained_upstreams(os.environ), **cfg.upstreams}
    with local_recorder(ledgers, root / "keys", run, cfg.sink, upstreams) as recorder:
        url = local_url(recorder.url, recorder.key, run)
        env = environment(cli, url, recorder.key, run, os.environ, True, own_secrets(cfg))
        env.update(local_defaults(cli, upstreams, UPSTREAMS["anthropic"], os.environ))
        extra = arguments(cli, recorder.url, run, local=True)
        own, more = cli_arguments(cli, args)
        command = [exe or cli, *extra, *own]
        _buckle_up([f"recording {cli} on this machine ({where})", *warnings, *more])
        try:
            code = _spawn(command, env)
        finally:
            badge.unbuckle()
            recorder.end(run)
    if recorder.written:
        # quoted to paste into a shell: the macOS data folder, Application Support, has a space
        paths = "\n".join(f"  file:      {shlex.quote(str(p))}" for p in recorder.written)
        say(f"recorded run {run}\n  replay it: seatbelt reconstruct {run}\n{paths}")
    else:
        say(f"nothing recorded ({cli} sent no model requests)")
    return code
