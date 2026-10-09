"""What `seatbelt run claude` gives Claude Code through `--settings`: a hook that stops a
denied tool before it runs, and a status line that ends with seatbelt's badge."""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from seatbelt.gateway.claude_code import (
    RECORDING,
    UNDER_POLICY,
    denial,
    launch_arguments,
    matcher,
    merged,
    settings,
    status_line,
    stopped_before_run,
)

BADGE = "<badge>"


def _run(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this module, as Claude Code runs it
        [sys.executable, "-m", "seatbelt.gateway.claude_code", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


# -- what the gateway accepts --------------------------------------------------


def test_the_hooks_report_is_recognised_as_claude_code_sends_it() -> None:
    reason = denial("Bash")
    # as Claude Code 2.1 sends it, checked end to end with 2.1.294
    assert stopped_before_run("Bash", f"PreToolUse:Bash hook error: {reason}", True)
    assert stopped_before_run("Bash", reason, True)
    assert stopped_before_run("Bash", [{"type": "text", "text": reason}], True)
    assert stopped_before_run("Bash", reason + "\n", True)


@pytest.mark.parametrize(
    ("tool", "content", "is_error"),
    [
        ("Bash", denial("Bash"), False),  # not reported as an error
        ("Write", denial("Bash"), True),  # another tool's refusal
        ("Bash", denial("Bash") + "\nfile.txt", True),  # the refusal and what the tool printed
        ("Bash", "file.txt\n" + denial("Bash"), True),
        ("Bash", "PreToolUse:Bash hook error: rm -rf done", True),
        ("Bash", [{"type": "text", "text": denial("Bash")}, {"type": "image"}], True),
        ("Bash", [], True),
        ("Bash", None, True),
    ],
)
def test_anything_else_is_not_the_hooks_report(tool: str, content: Any, is_error: bool) -> None:
    assert not stopped_before_run(tool, content, is_error)


# -- settings ------------------------------------------------------------------


def test_a_matcher_names_exactly_the_denied_tools() -> None:
    assert matcher(["Write", "Bash", "Bash"]) == "Bash|Write"  # exact names to Claude Code
    # any other character makes it an unanchored regex to Claude Code: anchored and escaped
    assert matcher(["mcp__db.query", "Bash"]) == r"^(?:Bash|mcp__db\.query)$"


def test_settings_stop_the_denied_tools_and_wrap_the_users_status_line() -> None:
    out = settings(["Write", "Bash"], {"type": "command", "command": "~/line.sh", "padding": 1})
    ((entry,),) = [out["hooks"]["PreToolUse"]]
    assert entry["matcher"] == "Bash|Write"
    ((hook,),) = [entry["hooks"]]
    assert hook["type"] == "command"
    assert hook["command"].endswith("-m seatbelt.gateway.claude_code hook Bash Write")
    line = out["statusLine"]
    assert line["padding"] == 1 and line["type"] == "command"
    assert line["command"].endswith("claude_code statusline --note recording '~/line.sh'")
    assert "hooks" not in settings([], None)  # no policy: a status line only
    assert settings([], None)["statusLine"]["command"].endswith("statusline --note recording")
    policed = settings([], None, UNDER_POLICY)["statusLine"]["command"]
    assert policed.endswith("statusline --note 'recording · org policy'")


def test_the_users_own_settings_keep_their_hooks_beside_seatbelts() -> None:
    theirs: dict[str, Any] = {
        "model": "x",
        "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": []}], "Stop": [{"hooks": []}]},
    }
    out = merged(theirs, settings(["Bash"], None))
    assert out["model"] == "x" and out["hooks"]["Stop"] == [{"hooks": []}]
    assert [e["matcher"] for e in out["hooks"]["PreToolUse"]] == ["Edit", "Bash"]
    assert "statusLine" in out


def _settings_arg(args: list[str]) -> dict[str, Any]:
    assert args[0] == "--settings"
    return json.loads(args[1])


def test_seatbelts_settings_go_first(tmp_path: Path) -> None:
    args, warnings = launch_arguments(["-p", "hi"], ["Bash"], tmp_path / "cfg", tmp_path)
    assert args[2:] == ["-p", "hi"] and warnings == []
    assert _settings_arg(args)["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


def test_the_users_own_settings_flag_is_merged_in(tmp_path: Path) -> None:
    inline = json.dumps({"statusLine": {"type": "command", "command": "mine"}, "model": "m"})
    args, _ = launch_arguments(
        ["--settings", "{}", "-p", "--settings", inline, "hi"], ["Bash"], tmp_path, tmp_path
    )
    given = _settings_arg(args)
    assert args[2:] == ["-p", "hi"] and given["model"] == "m"
    assert given["statusLine"]["command"].endswith("--note recording mine")
    (tmp_path / "s.json").write_text(json.dumps({"env": {"A": "1"}}))
    args, _ = launch_arguments(["--settings=s.json"], [], tmp_path, tmp_path)
    assert _settings_arg(args)["env"] == {"A": "1"} and len(args) == 2
    # after `--`, an argument is someone else's: an MCP server's, say
    args, _ = launch_arguments(
        ["mcp", "add", "x", "--", "srv", "--settings", "y"], [], tmp_path, tmp_path
    )
    assert args[2:] == ["mcp", "add", "x", "--", "srv", "--settings", "y"]


def test_settings_that_cannot_be_read_are_left_as_given_with_a_warning(tmp_path: Path) -> None:
    given = ["--settings", "missing.json", "-p", "hi"]
    args, (warning,) = launch_arguments(given, ["Bash"], tmp_path, tmp_path)
    assert args == given
    assert "missing.json" in warning and "stops a denied tool" in warning


def test_the_status_line_in_effect_is_the_one_wrapped(tmp_path: Path) -> None:
    user, project = tmp_path / "cfg", tmp_path / "project" / ".claude"
    user.mkdir()
    project.mkdir(parents=True)
    (user / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "user-line"}})
    )
    args, _ = launch_arguments([], [], user, project.parent)
    assert _settings_arg(args)["statusLine"]["command"].endswith("recording user-line")
    (project / "settings.local.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "local-line"}})
    )
    args, _ = launch_arguments([], [], user, project.parent)
    assert _settings_arg(args)["statusLine"]["command"].endswith("recording local-line")


def test_disable_all_hooks_is_warned_about(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    project = tmp_path / ".claude" / "settings.json"
    project.write_text(json.dumps({"disableAllHooks": True}))
    _, (warning,) = launch_arguments([], ["Bash"], tmp_path / "cfg", tmp_path)
    assert str(project) in warning and "a tool the org's policy denies runs" in warning
    _, (warning,) = launch_arguments([], [], tmp_path / "cfg", tmp_path)
    assert "no seatbelt status line" in warning and "denies" not in warning
    # a higher layer turns them back on
    _, warnings = launch_arguments(
        ["--settings", '{"disableAllHooks": false}'], ["Bash"], tmp_path / "cfg", tmp_path
    )
    assert warnings == []


# -- the hook and the status line, as Claude Code runs them ----------------------


def test_the_hook_denies_a_denied_tool_and_nothing_else() -> None:
    done = _run("hook", "Bash", "Write", stdin=json.dumps({"tool_name": "Bash", "tool_input": {}}))
    assert done.returncode == 0
    assert json.loads(done.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": denial("Bash"),
        }
    }
    other = _run("hook", "Bash", stdin=json.dumps({"tool_name": "Read"}))
    assert (other.returncode, other.stdout) == (0, "")  # no decision: Claude Code goes on
    garbled = _run("hook", "Bash", stdin="not json")
    assert (garbled.returncode, garbled.stdout) == (0, "")


def test_the_status_line_ends_with_the_badge() -> None:
    data = json.dumps({"model": {"display_name": "Sonnet"}}).encode()
    assert status_line(None, data, BADGE) == BADGE
    assert status_line(None, data, BADGE, RECORDING) == f"{BADGE} recording"
    assert status_line("echo mine", data, BADGE, RECORDING) == f"mine | {BADGE} recording"
    assert status_line("echo mine", data, BADGE) == f"mine | {BADGE}"
    name = 'import json, sys; print(json.load(sys.stdin)["model"]["display_name"])'
    reads_stdin = shlex.join([sys.executable, "-c", name])
    assert status_line(reads_stdin, data, BADGE) == f"Sonnet | {BADGE}"
    assert status_line("printf 'one\\ntwo\\n'", data, BADGE) == f"one\ntwo | {BADGE}"
    assert status_line("echo mine; exit 1", data, BADGE) == BADGE  # Claude Code would show none
    assert status_line("true", data, BADGE) == BADGE


def test_the_status_line_command_prints_the_badge() -> None:
    done = _run("statusline", "--note", UNDER_POLICY, "echo mine", stdin="{}")
    # as issue #46 drew it: the user's line, then the badge and what seatbelt is doing
    badge = "\x1b[1;7m seatbelt \x1b[0m"
    assert done.returncode == 0 and done.stdout == f"mine | {badge} recording · org policy\n"
    alone = _run("statusline", stdin="{}")
    assert alone.returncode == 0 and alone.stdout == f"{badge}\n"
    assert _run("nonsense").returncode == 2
