from pathlib import Path

import pytest

from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.policy.engine import Policy, PolicyDenied, Rule, allowlist, denylist
from seatbelt.record.recorder import Recorder


def test_allowed_call_records_a_check_per_rule_and_runs(tmp_path: Path) -> None:
    policy = Policy(allowlist("echo"), Rule("small", lambda _t, a: None if a["x"] < 10 else "big"))
    ran = False
    with (
        Recorder.start(tmp_path, agent_id="bot", run_id="ok", policy=policy) as rec,
        rec.tool_call("echo", {"x": 1}) as tool,
    ):
        ran = True
        tool.result({"x": 1})
    events = list(read_events(tmp_path / "ok.jsonl"))
    call, *checks, result = events[1:-1]
    assert ran
    assert call.kind == Kind.TOOL_CALL
    assert [c.kind for c in checks] == [Kind.POLICY_CHECK, Kind.POLICY_CHECK]
    assert [c.actor.id for c in checks] == ["allowlist", "small"]
    assert all(c.parent_id == call.id and c.attrs["policy.allowed"] for c in checks)
    assert result.kind == Kind.TOOL_RESULT


def test_denied_call_is_recorded_then_raised_before_the_tool_runs(tmp_path: Path) -> None:
    policy = Policy(denylist("rm"), allowlist("echo"))
    with (
        Recorder.start(tmp_path, agent_id="bot", run_id="deny", policy=policy) as rec,
        pytest.raises(PolicyDenied) as info,
        rec.tool_call("rm", {"path": "/"}),
    ):
        raise AssertionError("tool body must not run")
    events = list(read_events(tmp_path / "deny.jsonl"))
    call, first, second = events[1:4]
    assert info.value.rule == "denylist"
    assert info.value.call.id == call.id
    assert first.attrs == {"policy.allowed": False, "policy.reason": "rm is denied"}
    assert second.attrs == {"policy.allowed": False, "policy.reason": "rm is not allowed"}
    assert Kind.TOOL_RESULT not in [e.kind for e in events]
    assert events[-1].attrs["run.ok"] is True  # the run carries on after a denial


def test_no_policy_records_no_checks(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="bot", run_id="none") as rec, rec.tool_call("x", {}):
        pass
    assert Kind.POLICY_CHECK not in [e.kind for e in read_events(tmp_path / "none.jsonl")]


def test_deny_reason_is_redacted(tmp_path: Path) -> None:
    leak = Rule("leak", lambda _t, _a: "token sk-ant-abcdefghijklmnopqrstuvwxyz1234")
    with (
        Recorder.start(tmp_path, agent_id="bot", run_id="leak", policy=Policy(leak)) as rec,
        pytest.raises(PolicyDenied),
        rec.tool_call("x", {}),
    ):
        pass
    assert "sk-ant-" not in (tmp_path / "leak.jsonl").read_text()


def test_rules_see_the_arguments_the_tool_will_run_with(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []
    spy = Rule("spy", lambda _t, a: seen.append(a) and None)
    args: dict[str, object] = {"key": "sk-ant-abcdefghijklmnopqrstuvwxyz1234", "tup": (1, 2)}
    with (
        Recorder.start(tmp_path, agent_id="bot", run_id="raw", policy=Policy(spy)) as rec,
        rec.tool_call("x", args),
    ):
        pass
    assert seen[0] is args  # not the redacted, JSON-normalised copy in the ledger


def test_rule_that_raises_is_a_denial_and_later_rules_still_run(tmp_path: Path) -> None:
    policy = Policy(Rule("boom", lambda _t, a: a["missing"]), allowlist("x"))
    with (
        Recorder.start(tmp_path, agent_id="bot", run_id="boom", policy=policy) as rec,
        pytest.raises(PolicyDenied) as info,
        rec.tool_call("x", {}),
    ):
        pass
    checks = [e for e in read_events(tmp_path / "boom.jsonl") if e.kind == Kind.POLICY_CHECK]
    assert [c.actor.id for c in checks] == ["boom", "allowlist"]
    assert checks[0].attrs == {"policy.allowed": False, "policy.reason": "KeyError: 'missing'"}
    assert checks[1].attrs["policy.allowed"] is True
    assert info.value.rule == "boom"
