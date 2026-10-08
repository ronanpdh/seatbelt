"""What `seatbelt run claude` gives Claude Code through `--settings`, and what the gateway
expects back from it.

- A PreToolUse hook for the tools the org's policy denies, so they stop before they run.
  Claude Code sends the hook's reason as the tool's result, which the gateway accepts in the
  history (`stopped_before_run`); a result from a tool that ran is still refused.
- A status line that ends with seatbelt's badge, after the user's own.

Claude Code runs this module (`python -m seatbelt.gateway.claude_code`) for each denied tool
call and each status line refresh, and the launcher imports it: stdlib only. Sources and
what was checked against Claude Code 2.1: docs/plans/2026-10-08-visible-seatbelt-design.md"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

USER_AGENT = "claude-cli/"  # Claude Code's user-agent, and the Agent SDK's, which runs it
SETTINGS_FLAG = "--settings"
# a hook's reason, as Claude Code 2.1 sends it for a PreToolUse hook that denies
_HOOK_ERROR = "PreToolUse:{tool} hook error: "
STATUS_TIMEOUT = 5.0  # seconds the user's own status line has to answer
# names of only these are joined with `|`, which Claude Code reads as exact names (as it
# does spaces and `,`); a matcher with any other character is a regex to it
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_-]+")
_REGEX_SPECIAL = re.compile(r"[\\^$.*+?()\[\]{}|/]")
# the statusLine keys that change how Claude Code shows it, kept from the user's
_STATUS_KEYS = ("padding", "refreshInterval", "hideVimModeIndicator")


def is_claude_code(user_agent: str | None) -> bool:
    return (user_agent or "").startswith(USER_AGENT)


def denial(tool: str) -> str:
    """The reason the hook gives for stopping `tool`, which the model reads as its result."""
    return f"[seatbelt] {tool} did not run: the org's policy denies it"


def _text(content: Any) -> str | None:
    """A tool result's content as text: a string, or a list of text blocks only."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    texts: list[str] = []
    for block in cast(list[Any], content):
        if not isinstance(block, dict):
            return None
        b = cast(dict[str, Any], block)
        if b.get("type") != "text" or not isinstance(b.get("text"), str):
            return None
        texts.append(cast(str, b["text"]))
    return "".join(texts)


def stopped_before_run(tool: str, content: Any, is_error: bool) -> bool:
    """Whether a tool result is Claude Code's report that seatbelt's hook stopped `tool`: an
    error whose text is the hook's reason and nothing else, so nothing the tool did is in it."""
    text = _text(content)
    if not is_error or text is None:
        return False
    reason = denial(tool)
    return text.strip() in (reason, _HOOK_ERROR.format(tool=tool) + reason)


def matcher(tools: Collection[str]) -> str:
    """A PreToolUse matcher for exactly `tools`: their names joined by `|`, which Claude Code
    reads as exact names, or an anchored regex when a name has other characters (Claude Code
    reads those as an unanchored JavaScript regex)."""
    names = sorted(set(tools))
    if all(_PLAIN_NAME.fullmatch(n) for n in names):
        return "|".join(names)
    return "^(?:" + "|".join(_REGEX_SPECIAL.sub(r"\\\g<0>", n) for n in names) + ")$"


def _command(*args: str) -> str:
    """A shell command that runs this module with this Python: Claude Code runs it in a shell."""
    return shlex.join([sys.executable, "-m", __name__, *args])


def settings(denied: Collection[str], status_line: Mapping[str, Any] | None) -> dict[str, Any]:
    """What seatbelt adds to Claude Code's settings: the hook for the `denied` tools, if any,
    and a status line that runs `status_line`, the user's own if any, before the badge."""
    out: dict[str, Any] = {}
    if denied:
        hook = {"type": "command", "command": _command("hook", *sorted(set(denied)))}
        out["hooks"] = {"PreToolUse": [{"matcher": matcher(denied), "hooks": [hook]}]}
    line = dict(status_line or {})
    own = line.get("command") if line.get("type") == "command" else None
    out["statusLine"] = {
        **{k: line[k] for k in _STATUS_KEYS if k in line},
        "type": "command",
        "command": _command("statusline", *([own] if isinstance(own, str) and own else [])),
    }
    return out


def merged(theirs: Mapping[str, Any], ours: Mapping[str, Any]) -> dict[str, Any]:
    """The user's own `--settings` with seatbelt's added: Claude Code reads only the last
    `--settings` it is given. Their hooks stay beside seatbelt's; seatbelt's status line,
    which runs theirs, takes its place."""
    out = {**theirs, **{k: v for k, v in ours.items() if k != "hooks"}}
    hooks: dict[str, Any] = dict(cast(dict[str, Any], theirs.get("hooks") or {}))
    for event, entries in cast(dict[str, list[Any]], ours.get("hooks", {})).items():
        existing: Any = hooks.get(event)
        before = cast(list[Any], existing) if isinstance(existing, list) else []
        hooks[event] = [*before, *entries]
    if hooks:
        out["hooks"] = hooks
    return out


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


def _flag_value(args: Sequence[str]) -> tuple[list[str], str | None]:
    """`args` without any `--settings` before a `--`, and the last one's value (Claude Code
    reads only the last). After a `--` an argument is someone else's, e.g. an MCP server's."""
    rest: list[str] = []
    value: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            rest += args[i:]
            break
        if arg == SETTINGS_FLAG and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        if arg.startswith(SETTINGS_FLAG + "="):
            value = arg[len(SETTINGS_FLAG) + 1 :]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest, value


def given_settings(value: str, cwd: Path) -> dict[str, Any] | None:
    """The settings a `--settings` value gives: inline JSON, or a JSON file's path. None
    when they cannot be read."""
    if value.lstrip().startswith("{"):
        try:
            data: Any = json.loads(value)
        except ValueError:
            return None
        return cast(dict[str, Any], data) if isinstance(data, dict) else None
    return _read_json(cwd / Path(value).expanduser())


def settings_files(config_dir: Path, cwd: Path) -> list[Path]:
    """Claude Code's settings files `--settings` overrides, lowest precedence first."""
    project = cwd / ".claude"
    return [
        config_dir / "settings.json",
        project / "settings.json",
        project / "settings.local.json",
    ]


def effective(layers: Sequence[tuple[str, Mapping[str, Any]]], key: str) -> tuple[str, Any]:
    """(where, value) of `key` in the highest layer that sets it; ("", None) if none does."""
    for where, layer in reversed(layers):
        if key in layer:
            return where, layer[key]
    return "", None


def launch_arguments(
    args: Sequence[str], denied: Collection[str], config_dir: Path, cwd: Path
) -> tuple[list[str], list[str]]:
    """(Claude Code's arguments with seatbelt's `--settings` first, warnings to show). A
    `--settings` of the user's own is merged into seatbelt's, or left as it is, with a
    warning, when it cannot be read."""
    rest, value = _flag_value(args)
    layers: list[tuple[str, Mapping[str, Any]]] = [
        (str(p), s) for p in settings_files(config_dir, cwd) if (s := _read_json(p)) is not None
    ]
    theirs: dict[str, Any] = {}
    if value is not None:
        given = given_settings(value, cwd)
        if given is None:
            return list(args), [
                f"could not read {SETTINGS_FLAG} {value}: Claude Code runs without seatbelt's "
                "status line" + (" and the hook that stops a denied tool" if denied else "")
            ]
        theirs = given
        layers.append((SETTINGS_FLAG, given))
    warnings: list[str] = []
    where, disabled = effective(layers, "disableAllHooks")
    if disabled is True:
        stops = "; a tool the org's policy denies runs, and the gateway then refuses the rest of "
        warnings.append(
            f"disableAllHooks is set in {where}: Claude Code shows no seatbelt status line"
            + (f"{stops}the conversation" if denied else "")
        )
    _, status_line = effective(layers, "statusLine")
    ours = settings(
        denied, cast(dict[str, Any], status_line) if isinstance(status_line, dict) else None
    )
    combined = merged(theirs, ours) if value is not None else ours
    return [SETTINGS_FLAG, json.dumps(combined, separators=(",", ":")), *rest], warnings


def _hook(denied: Collection[str]) -> int:
    """PreToolUse: deny a call to one of the `denied` tools. Anything else, or input that
    cannot be read, gets no decision: Claude Code goes on, and the gateway's refusal stays."""
    try:
        data: Any = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = cast(dict[str, Any], data).get("tool_name") if isinstance(data, dict) else None
    if isinstance(tool, str) and tool in denied:
        decision = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": denial(tool),
        }
        print(json.dumps({"hookSpecificOutput": decision}))
    return 0


def status_line(own: str | None, data: bytes, badge: str) -> str:
    """The status line: the user's own (`own`, a shell command given Claude Code's JSON on
    stdin, as Claude Code runs it), then ` | ` and seatbelt's badge on its last line."""
    lines: list[str] = []
    if own:
        try:
            done = subprocess.run(  # noqa: S602 - the user's own status line, as Claude Code runs it
                own, shell=True, input=data, capture_output=True, timeout=STATUS_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0:  # Claude Code shows nothing otherwise
            lines = done.stdout.decode(errors="replace").rstrip("\n").split("\n")
    if lines and lines[-1].strip():
        lines[-1] += f" | {badge}"
    else:
        lines = [*lines[:-1], badge]
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    if argv[:1] == ["hook"]:
        return _hook(set(argv[1:]))
    if argv[:1] == ["statusline"]:
        from seatbelt.gateway.badge import BADGE

        own = argv[1] if len(argv) > 1 else None
        print(status_line(own, sys.stdin.buffer.read(), BADGE))
        return 0
    print(f"usage: python -m {__name__} hook TOOL... | statusline [COMMAND]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
