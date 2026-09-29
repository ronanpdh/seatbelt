"""`seatbelt import compliance` against a fake of the documented Compliance API.

Shapes follow the documented examples (platform.claude.com/docs/en/manage-claude/
compliance-sessions and compliance-content-data, read 2026-09-29); none were captured from a
live tenant."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from rich.console import Console
from tests.fake_compliance import KEY, FakeCompliance, at, error

from seatbelt.attest.sign import Signer
from seatbelt.compliance.client import (
    ComplianceClient,
    ComplianceError,
    ContentUnavailable,
    NotFound,
    TryLater,
)
from seatbelt.compliance.importer import LOCK, STATE, Busy, Importer
from seatbelt.gateway.config import ComplianceConfig, load_config
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import read_events
from seatbelt.locks import try_lock
from seatbelt.record.recorder import Recorder
from seatbelt.report.fleet import fleet
from seatbelt.report.timeline import timeline
from seatbelt.verify.attest import Attestation, verify_attestation
from seatbelt.verify.chain import verify_file

USER = {"id": "user_01Gp", "email_address": "engineer@example.com"}


def _local_meta(
    sid: str, updated: str, surface: str | None = "cowork", **extra: Any
) -> dict[str, Any]:
    return {
        "type": "compliance_local_session",
        "id": sid,
        "organization_uuid": "9a1e0000-0000-0000-0000-000000000000",
        "workspace_id": None,
        "user": USER,
        "product_surface": surface,
        "created_at": at(0),
        "updated_at": updated,
        **extra,
    }


def _msg(mid: str, role: str, *content: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "compliance_local_session_message",
        "id": mid,
        "role": role,
        "model": "claude-opus-5-5" if role == "assistant" else None,
        "created_at": at(0),
        "provenance": None,
        "content": list(content),
        **extra,
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "truncated": False}


def _use(tid: str | None, name: str, args: Any, truncated: bool = False) -> dict[str, Any]:
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"type": "tool_use", "id": tid, "name": name, "input": raw, "truncated": truncated}


def _result(tid: str | None, name: str, text: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tid,
        "name": name,
        "is_error": is_error,
        "content": [{"type": "text", "text": text}],
        "truncated": False,
    }


def _transcript() -> list[dict[str, Any]]:
    """The documented local-session example: a marker, a prompt, a tool call and its result,
    and an answer."""
    return [
        _msg(
            "clsm_marker",
            "user",
            {"type": "text", "text": "[system prompt content not shown]", "truncated": True},
            provenance={"type": "synthetic_marker"},
        ),
        _msg("clsm_1", "user", _text("Fix the failing test in tests/auth_test.py")),
        _msg(
            "clsm_2",
            "assistant",
            _text("I'll read the test file first."),
            _use("toolu_01", "Read", {"file_path": "tests/auth_test.py"}),
        ),
        _msg("clsm_3", "user", _result("toolu_01", "Read", "def test_login_expiry():\n    ...")),
        _msg("clsm_4", "assistant", _text("The test was asserting on a stale expiry timestamp.")),
    ]


class Clock:
    def __init__(self, minute: int) -> None:
        self.t = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=minute)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: int) -> None:
        self.t += timedelta(minutes=minutes)


def _importer(
    tmp_path: Path,
    fake: FakeCompliance,
    clock: Clock,
    signer: Signer | None = None,
    **cfg: Any,
) -> Importer:
    client = ComplianceClient(KEY, transport=fake.transport(), sleep=lambda _: None, now=clock)
    config = ComplianceConfig.model_validate({"sources": ["local_sessions"], **cfg})
    return Importer(config, tmp_path / "compliance", client, signer, now=clock)


def _ledgers(root: Path) -> dict[str, list[Event]]:
    return {p.stem: list(read_events(p)) for p in sorted(root.glob("*.jsonl"))}


# -- local sessions ----------------------------------------------------------------------------


def test_a_settled_local_session_becomes_one_signed_ledger(tmp_path: Path) -> None:
    fake = FakeCompliance()
    fake.local["clls_01"] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    signer = Signer.generate()
    summary = _importer(tmp_path, fake, Clock(120), signer).run()

    root = tmp_path / "compliance"
    (path,) = root.glob("*.jsonl")
    assert path.stem == "cowork-clls_01-1"
    assert summary.imported == {"local_session": 1} and summary.messages == {"local_session": 5}
    assert verify_file(path).ok
    pub = tmp_path / "k.pub"
    pub.write_bytes(signer.public_pem())
    assert verify_attestation(path, pub).status is Attestation.ATTESTED
    events = list(read_events(path))
    start = events[0].attrs
    assert start["principal.id"] == "user_01Gp" and start["principal.auth"] == "compliance_api"
    assert start["principal.name"] == "engineer@example.com"  # from the list, not the messages
    assert start["compliance.source"] == "local_session"
    assert start["compliance.product_surface"] == "cowork"
    assert start["compliance.previous"] is None and start["compliance.segment"] == 1
    assert start["compliance.endpoint"] == "/v1/compliance/apps/sessions/local/clls_01/messages"
    assert start["compliance.query"]["tool_result_max_bytes"] == -1
    assert start["compliance.request_ids"]  # the responses this ledger was built from
    kinds = [e.kind for e in events[1:-1]]
    assert kinds == [
        Kind.USER_MESSAGE,  # the marker, flagged
        Kind.USER_MESSAGE,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,
        Kind.MODEL_RESPONSE,
    ]
    marker, prompt, answer, call, result, _ = events[1:-1]
    assert marker.attrs["compliance.provenance"] == {"type": "synthetic_marker"}
    assert prompt.actor.id == "user_01Gp"
    assert prompt.attrs["compliance.message_id"] == "clsm_1"
    assert answer.attrs["gen_ai.response.model"] == "claude-opus-5-5"
    assert call.parent_id == answer.id
    assert call.attrs["gen_ai.tool.call.arguments"] == {"file_path": "tests/auth_test.py"}
    assert result.parent_id == call.id and result.attrs["error"] is None
    assert not any(e.kind is Kind.MODEL_REQUEST for e in events)  # none were seen
    # every request carried the key and version, and asked for whole tool blocks
    assert all(r.headers["x-api-key"] == KEY for r in fake.requests)
    messages = [r for r in fake.requests if r.url.path.endswith("/messages")]
    assert all(r.url.params["tool_use_input_max_bytes"] == "-1" for r in messages)


def test_a_session_that_grows_gets_a_chained_segment_of_only_new_messages(tmp_path: Path) -> None:
    fake = FakeCompliance()
    session: dict[str, Any] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    fake.local["clls_01"] = session
    clock = Clock(120)
    _importer(tmp_path, fake, clock).run()
    assert _importer(tmp_path, fake, clock).run().written == []  # nothing new

    session["messages"].append(_msg("clsm_5", "user", _text("thanks")))
    session["meta"]["updated_at"] = at(130)
    clock.advance(120)
    _importer(tmp_path, fake, clock).run()

    ledgers = _ledgers(tmp_path / "compliance")
    assert list(ledgers) == ["cowork-clls_01-1", "cowork-clls_01-2"]
    second = ledgers["cowork-clls_01-2"]
    assert second[0].attrs["compliance.previous"]["run_id"] == "cowork-clls_01-1"
    assert [e.attrs["compliance.message_id"] for e in second[1:-1]] == ["clsm_5"]


def test_a_session_not_quiet_yet_is_imported_once_it_settles_though_no_longer_listed(
    tmp_path: Path,
) -> None:
    """The list window moves past a session whose last call was before it; the pending list
    still finds it."""
    fake = FakeCompliance()
    fake.local["clls_01"] = {"meta": _local_meta("clls_01", at(100)), "messages": _transcript()}
    clock = Clock(110)  # 10 minutes quiet: not settled
    assert _importer(tmp_path, fake, clock).run().written == []
    clock.advance(6)  # listed again (the window reaches back 15 minutes), still not settled
    assert _importer(tmp_path, fake, clock).run().written == []
    clock.advance(114)  # the next window starts after its last update
    summary = _importer(tmp_path, fake, clock).run()
    assert summary.listed["local_sessions"] == 0  # not in the window any more
    assert [p.stem for p in summary.written] == ["cowork-clls_01-1"]
    assert any(r.url.path.endswith("/local/clls_01") for r in fake.requests)


def test_surfaces_filter_and_run_ids_stay_valid(tmp_path: Path) -> None:
    fake = FakeCompliance()
    for sid, surface in (("a", "office_agents/excel"), ("b", None), ("c", "claude_code")):
        fake.local[sid] = {"meta": _local_meta(sid, at(10), surface), "messages": _transcript()}
    _importer(tmp_path, fake, Clock(120), surfaces=["office_agents/excel", "cowork"]).run()
    assert list(_ledgers(tmp_path / "compliance")) == ["office_agents.excel-a-1"]
    _importer(tmp_path / "all", fake, Clock(120)).run()
    assert list(_ledgers(tmp_path / "all" / "compliance")) == [
        "claude_code-c-1",
        "office_agents.excel-a-1",
        "unknown-b-1",
    ]


def test_null_tool_ids_truncated_input_and_errors_are_kept(tmp_path: Path) -> None:
    fake = FakeCompliance()
    messages = [
        _msg("m1", "user", _text("go")),
        _msg(
            "m2",
            "assistant",
            _use(None, "Bash", {"cmd": "ls"}),
            _use("toolu_2", "Write", '{"path": "a", "content": "xx…[truncated', truncated=True),
        ),
        _msg(
            "m3", "user", _result(None, "Bash", "a b"), _result("toolu_2", "Write", "denied", True)
        ),
    ]
    fake.local["s"] = {"meta": _local_meta("s", at(10)), "messages": messages}
    _importer(tmp_path, fake, Clock(120)).run()
    events = next(iter(_ledgers(tmp_path / "compliance").values()))
    calls = [e for e in events if e.kind is Kind.TOOL_CALL]
    results = [e for e in events if e.kind is Kind.TOOL_RESULT]
    assert calls[0].attrs["gen_ai.tool.call.id"] == "m2#0"  # no id: message and position
    assert calls[1].attrs["gen_ai.tool.call.arguments"] == {}  # truncated JSON is not parsed
    assert calls[1].attrs["compliance.input"].startswith('{"path"')
    assert calls[1].attrs["compliance.truncated"] is True
    assert results[0].parent_id == calls[0].id  # by name, the oldest unanswered call
    assert results[1].parent_id == calls[1].id and results[1].attrs["error"] == "denied"


def test_a_run_killed_before_signing_is_closed_and_carried_on_from(tmp_path: Path) -> None:
    fake = FakeCompliance()
    session: dict[str, Any] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    fake.local["clls_01"] = session
    root = tmp_path / "compliance"
    signer = Signer.generate()
    # a run that wrote two messages of the first segment, then died
    crashed = Recorder.start(
        root,
        agent_id="compliance:cowork",
        run_id="cowork-clls_01-1",
        metadata={"compliance.id": "clls_01", "principal.id": "user_01Gp"},
        signer=signer,
    ).__enter__()
    crashed.user_message("user_01Gp", "marker", {"compliance.message_id": "clsm_marker"})
    crashed.user_message("user_01Gp", "prompt", {"compliance.message_id": "clsm_1"})

    summary = _importer(tmp_path, fake, Clock(120), signer).run()
    assert summary.closed == ["cowork-clls_01-1"]
    ledgers = _ledgers(root)
    first = ledgers["cowork-clls_01-1"]
    assert first[-1].kind is Kind.RUN_END and first[-1].attrs["run.error"] == "importer killed"
    # what the killed run had not recorded, from its last message on: that one may be partial
    second = ledgers["cowork-clls_01-2"]
    ids = [e.attrs["compliance.message_id"] for e in second if "compliance.message_id" in e.attrs]
    assert ids[:2] == ["clsm_1", "clsm_2"] and "clsm_marker" not in ids


def test_a_lost_state_file_records_nothing_twice(tmp_path: Path) -> None:
    fake = FakeCompliance()
    session: dict[str, Any] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    fake.local["clls_01"] = session
    clock = Clock(120)
    _importer(tmp_path, fake, clock).run()
    (tmp_path / "compliance" / STATE).unlink()
    session["messages"].append(_msg("clsm_5", "user", _text("thanks")))
    session["meta"]["updated_at"] = at(130)
    clock.advance(120)
    _importer(tmp_path, fake, clock).run()
    second = _ledgers(tmp_path / "compliance")["cowork-clls_01-2"]
    assert [e.attrs["compliance.message_id"] for e in second[1:-1]] == ["clsm_5"]


def test_a_transcript_whose_last_recorded_message_is_gone_writes_nothing(tmp_path: Path) -> None:
    fake = FakeCompliance()
    session: dict[str, Any] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    fake.local["clls_01"] = session
    clock = Clock(120)
    _importer(tmp_path, fake, clock).run()
    session["messages"] = [_msg("other", "user", _text("rewritten"))]
    session["meta"]["updated_at"] = at(130)
    clock.advance(120)
    summary = _importer(tmp_path, fake, clock).run()
    assert summary.stuck == ["local_session:clls_01"] and not summary.ok
    assert list(_ledgers(tmp_path / "compliance")) == ["cowork-clls_01-1"]


def test_turns_recorded_then_aged_out_leave_the_rest_new(tmp_path: Path) -> None:
    """With a finite retention period the oldest turns age out, a placeholder with a new id
    each time stands in for them, and the last turn recorded can be among them."""
    placeholder = {"type": "content_unavailable", "reason": "retention_elapsed"}
    fake = FakeCompliance()
    session: dict[str, Any] = {
        "meta": _local_meta("clls_01", at(10)),
        "messages": [_msg("clsm_r1", "user", provenance=placeholder), *_transcript()[1:]],
    }
    fake.local["clls_01"] = session
    clock = Clock(120)
    _importer(tmp_path, fake, clock).run()
    first = _ledgers(tmp_path / "compliance")["cowork-clls_01-1"]
    assert first[1].attrs["compliance.provenance"] == placeholder  # recorded at the start
    # every recorded turn ages out; then the session grows
    session["messages"] = [
        _msg("clsm_r2", "user", provenance=placeholder),
        _msg("clsm_5", "user", _text("thanks")),
    ]
    session["meta"]["updated_at"] = at(130)
    clock.advance(120)
    summary = _importer(tmp_path, fake, clock).run()
    assert summary.ok
    second = _ledgers(tmp_path / "compliance")["cowork-clls_01-2"]
    assert [e.attrs["compliance.message_id"] for e in second[1:-1]] == ["clsm_5"]


def test_two_imports_on_one_folder_do_not_run_at_once(tmp_path: Path) -> None:
    root = tmp_path / "compliance"
    root.mkdir()
    with (root / LOCK).open("wb") as held:
        assert try_lock(held)
        with pytest.raises(Busy):
            _importer(tmp_path, FakeCompliance(), Clock(0)).run()


def test_a_session_to_try_later_stays_pending(tmp_path: Path) -> None:
    fake = FakeCompliance()
    fake.local["clls_01"] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    fake.faults.append(
        (
            lambda r: r.url.path.endswith("/messages"),
            error(503, "Local sessions are temporarily unavailable. Try again later."),
        )
    )
    clock = Clock(120)
    summary = _importer(tmp_path, fake, clock).run()
    assert summary.skipped == ["local_session:clls_01"] and summary.written == []
    clock.advance(30)
    assert len(_importer(tmp_path, fake, clock).run().written) == 1


# -- remote sessions ---------------------------------------------------------------------------


def _remote_meta(sid: str, status: str, created: str, updated: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "organization_uuid": "91012d09-e48b-438e-a489-1bebfd8fa6f9",
        "user": USER,
        "agent_id": None,
        "started_by_user": None,
        "status": status,
        "created_at": created,
        "updated_at": updated,
        "product_surface": "cowork_remote",
        "claude_project_id": None,
        **extra,
    }


def _remote_msg(mid: str, role: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "role": role,
        "created_at": at(0),
        "content": [_text(text)] if text else [],
        "sent_by_user_id": None,
        "content_unavailable": False,
        **extra,
    }


def test_remote_sessions_by_status_and_owner(tmp_path: Path) -> None:
    fake = FakeCompliance()
    starter = {"id": "user_02", "email_address": "lead@example.com"}
    fake.remote["cse_done"] = {
        "meta": _remote_meta("cse_done", "archived", at(5), at(100)),  # final, however recent
        "messages": [
            _remote_msg("csev_1", "user", "Summarize the feedback"),
            _remote_msg("csev_2", "assistant", "", content_unavailable=True),
        ],
    }
    fake.remote["cse_agent"] = {
        "meta": _remote_meta(
            "cse_agent",
            "active",
            at(6),
            at(10),
            user=None,
            agent_id="cagt_01",
            started_by_user=starter,
            claude_project_id="claude_proj_01",
        ),
        "messages": [_remote_msg("csev_3", "user", "run the report", sent_by_user_id="user_03")],
    }
    fake.remote["cse_new"] = {
        "meta": _remote_meta("cse_new", "pending", at(7), at(110)),
        "messages": [],
    }
    clock = Clock(120)
    summary = _importer(tmp_path, fake, clock, sources=["remote_sessions"]).run()
    ledgers = _ledgers(tmp_path / "compliance")
    assert sorted(ledgers) == ["cowork_remote-cse_agent-1", "cowork_remote-cse_done-1"]
    assert summary.listed["remote_sessions"] == 3
    done = ledgers["cowork_remote-cse_done-1"]
    unavailable = done[-2]
    assert unavailable.kind is Kind.MODEL_RESPONSE
    assert unavailable.attrs["gen_ai.response.model"] is None  # remote messages carry none
    assert unavailable.attrs["compliance.provenance"] == {"type": "content_unavailable"}
    agent = ledgers["cowork_remote-cse_agent-1"]
    assert agent[0].attrs["principal.id"] == "user_02"  # who started the agent's run
    assert agent[0].attrs["compliance.agent_id"] == "cagt_01"
    assert agent[0].attrs["compliance.project_id"] == "claude_proj_01"  # from the list
    assert agent[1].actor.id == "user_03"  # who sent that message

    # the pending session is provisioned and finishes; the active one, still followed, is
    # deleted; the archived one is no longer followed
    fake.remote["cse_new"]["meta"].update(status="archived", updated_at=at(125))
    fake.remote["cse_new"]["messages"] = [_remote_msg("csev_9", "user", "hello")]
    del fake.remote["cse_agent"]
    clock.advance(10)
    _importer(tmp_path, fake, clock, sources=["remote_sessions"]).run()
    assert "cowork_remote-cse_new-1" in _ledgers(tmp_path / "compliance")
    state = json.loads((tmp_path / "compliance" / STATE).read_text())
    conversations = state["conversations"]
    assert conversations["remote_session:cse_agent"]["meta"]["status"] == "deleted"
    assert conversations["remote_session:cse_done"]["meta"]["status"] == "archived"


# -- chats -------------------------------------------------------------------------------------


def _chat(cid: str, updated: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "name": f"chat {cid}",
        "created_at": at(0),
        "updated_at": updated,
        "deleted_at": None,
        "href": f"https://claude.ai/chat/{cid}",
        "model": "claude-opus-5-5",
        "organization_uuid": "91012d09-e48b-438e-a489-1bebfd8fa6f9",
        "project_id": None,
        "user": USER,
        **extra,
    }


def _chat_messages() -> list[dict[str, Any]]:
    return [
        {
            "id": "claude_chat_msg_1",
            "role": "user",
            "created_at": at(0),
            "content": [{"type": "text", "text": "Draft requirements"}],
            "files": [
                {
                    "id": "claude_file_01",
                    "filename": "mockup.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 482133,
                    "md5": "56367e4d2705cc9c025ad07424e944f0",
                }
            ],
        },
        {
            "id": "claude_chat_msg_2",
            "role": "assistant",
            "created_at": at(1),
            "content": [
                {"type": "text", "text": "Here is a draft", "thinking_redacted": True},
            ],
            "artifacts": [
                {
                    "id": "claude_artifact_01",
                    "version_id": "claude_artifact_version_01",
                    "title": "Draft",
                    "artifact_type": "text/markdown",
                }
            ],
        },
    ]


def test_chats_walk_with_a_cursor_and_record_deletions(tmp_path: Path) -> None:
    fake = FakeCompliance()
    for n in range(3):
        cid = f"claude_chat_{n}"
        fake.chats[cid] = {"meta": _chat(cid, at(10 + n)), "messages": _chat_messages()}
    clock = Clock(120)
    summary = _importer(tmp_path, fake, clock, sources=["chats"]).run()
    assert summary.listed["chats"] == 3  # across two pages
    ledgers = _ledgers(tmp_path / "compliance")
    assert sorted(ledgers) == [f"chat-claude_chat_{n}-1" for n in range(3)]
    events = ledgers["chat-claude_chat_0-1"]
    assert events[0].attrs["run.name"] == "chat claude_chat_0"
    prompt, answer = events[1], events[2]
    assert prompt.attrs["compliance.files"][0]["md5"] == "56367e4d2705cc9c025ad07424e944f0"
    assert answer.attrs["gen_ai.response.model"] is None  # the chat's model is not per message
    assert answer.attrs["compliance.chat_model"] == "claude-opus-5-5"
    assert answer.attrs["compliance.thinking_redacted"] is True
    assert answer.attrs["compliance.artifacts"][0]["version_id"] == "claude_artifact_version_01"
    first_walk = [r for r in fake.requests if r.url.path.endswith("/chats")]
    assert all(r.url.params["order_by"] == "updated_at" for r in first_walk)

    # one chat is deleted in claude.ai: it comes back after the cursor, content gone
    fake.chats["claude_chat_1"]["meta"].update(deleted_at=at(130), updated_at=at(130), name="")
    fake.requests.clear()
    clock.advance(20)
    _importer(tmp_path, fake, clock, sources=["chats"]).run()
    assert "after_id" in fake.requests[0].url.params  # resumed from the saved cursor
    deleted = _ledgers(tmp_path / "compliance")["chat-claude_chat_1-2"]
    assert deleted[0].attrs["compliance.deleted_at"] == at(130)
    assert [e.kind for e in deleted] == [Kind.RUN_START, Kind.RUN_END]  # no messages


# -- the client --------------------------------------------------------------------------------


def _client(
    fake: FakeCompliance, waits: list[float], clock: Clock | None = None
) -> ComplianceClient:
    return ComplianceClient(
        KEY, transport=fake.transport(), sleep=waits.append, now=clock or Clock(0)
    )


def _always(_request: httpx2.Request) -> bool:
    return True


def test_the_client_follows_the_documented_retry_contract() -> None:
    fake = FakeCompliance()
    fake.local["s"] = {"meta": _local_meta("s", at(1)), "messages": []}
    path = "/v1/compliance/apps/sessions/local/s"
    waits: list[float] = []
    client = _client(fake, waits)

    fake.faults.append((_always, error(429, "rate limited", **{"retry-after": "25"})))
    assert client.get(path)["id"] == "s"
    assert waits == [25.0]  # honoured retry-after

    fake.faults.append((_always, error(500, "deterministic", **{"x-should-retry": "false"})))
    with pytest.raises(ComplianceError, match="deterministic"):
        client.get(path)

    waits.clear()
    fake.faults += [(_always, error(502, "bad gateway")), (_always, error(529, "overloaded"))]
    assert client.get(path)["id"] == "s"
    assert waits == [1.0, 2.0]  # exponential from 1 s

    fake.faults.append(
        (_always, error(503, "Local sessions are temporarily unavailable. Try again later."))
    )
    with pytest.raises(TryLater):
        client.get(path)

    fake.faults += [
        (_always, error(503, "Captured content is temporarily unavailable. Try again shortly."))
    ] * 7
    with pytest.raises(ContentUnavailable):
        client.get(path)

    with pytest.raises(NotFound):
        client.get("/v1/compliance/apps/sessions/local/missing")

    bad = ComplianceClient("wrong", transport=fake.transport(), sleep=waits.append)
    with pytest.raises(ComplianceError) as caught:
        bad.get(path)
    assert caught.value.status == 401 and "wrong" not in str(caught.value)


def test_the_remote_budgets_repeated_429_backs_off() -> None:
    fake = FakeCompliance()
    fake.remote["r"] = {"meta": _remote_meta("r", "active", at(0), at(0)), "messages": []}
    waits: list[float] = []
    client = _client(fake, waits)
    limited = error(429, "rate limited", **{"retry-after": "1"})
    fake.faults += [(_always, limited)] * 3
    client.get("/v1/compliance/apps/sessions/remote/r/messages")
    assert waits == [1.0, 2.0, 4.0]  # retry-after 1, then exponential as it repeats


def test_the_client_slows_before_the_shared_limit_runs_out() -> None:
    fake = FakeCompliance()
    fake.local["s"] = {"meta": _local_meta("s", at(1)), "messages": []}
    clock = Clock(0)
    ok = fake.handle

    def low(request: httpx2.Request) -> httpx2.Response:
        resp = ok(request)
        resp.headers["anthropic-ratelimit-requests-remaining"] = "3"
        resp.headers["anthropic-ratelimit-requests-reset"] = (
            clock() + timedelta(seconds=20)
        ).isoformat()
        return resp

    waits: list[float] = []
    client = ComplianceClient(
        KEY, transport=httpx2.MockTransport(low), sleep=waits.append, now=clock
    )
    client.get("/v1/compliance/apps/sessions/local/s")
    assert waits == [20.0]


# -- config, report and CLI --------------------------------------------------------------------


def test_the_compliance_block_needs_no_upstreams(tmp_path: Path) -> None:
    p = tmp_path / "g.yaml"
    p.write_text(
        "ledgers: runs\ncompliance:\n  since: 2026-09-01T00:00:00Z\n  surfaces: [cowork]\n"
    )
    cfg = load_config(p)
    assert cfg.compliance is not None and cfg.compliance.settle == 3600
    assert cfg.compliance_ledgers == tmp_path / "runs" / "compliance"
    p.write_text("ledgers: runs\ncompliance:\n  since: 2026-09-01T00:00:00\n")
    with pytest.raises(ValueError, match="UTC offset"):
        load_config(p)
    p.write_text("ledgers: runs\ncompliance:\n  sources: [activity]\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_the_report_counts_only_verified_imported_answers(tmp_path: Path) -> None:
    fake = FakeCompliance()
    messages = [
        *_transcript(),
        _msg("clsm_x", "assistant", _text("[claimed]"), provenance={"type": "client_asserted"}),
    ]
    fake.local["s"] = {"meta": _local_meta("s", at(10)), "messages": messages}
    fake.remote["r"] = {
        "meta": _remote_meta("r", "archived", at(0), at(10)),
        "messages": [_remote_msg("m", "assistant", "hi")],
    }
    _importer(tmp_path, fake, Clock(120), sources=["local_sessions", "remote_sessions"]).run()
    report = fleet(tmp_path / "compliance", None)
    assert report.by_model["claude-opus-5-5"].calls == 2  # not the client-asserted one
    assert report.by_model["unknown"].calls == 1  # a remote answer names no model
    assert report.by_principal["user_01Gp"].runs == 2


def test_the_cli_imports_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    import seatbelt.compliance.client as client_module
    from seatbelt.attest.sign import keygen
    from seatbelt.cli import app

    fake = FakeCompliance()
    fake.local["clls_01"] = {"meta": _local_meta("clls_01", at(10)), "messages": _transcript()}
    real = client_module.ComplianceClient

    class Faked(real):  # type: ignore[misc, valid-type]
        def __init__(self, key: str, url: str = "") -> None:
            super().__init__(key, url, transport=fake.transport(), sleep=lambda _: None)

    monkeypatch.setattr(client_module, "ComplianceClient", Faked)
    keygen(tmp_path / "keys")
    cfg = tmp_path / "g.yaml"
    cfg.write_text("signing_key: keys/seatbelt.key\nledgers: runs\ncompliance: {}\n")
    runner = CliRunner()
    monkeypatch.delenv("ANTHROPIC_COMPLIANCE_ACCESS_KEY", raising=False)
    out = runner.invoke(app, ["import", "compliance", "--config", str(cfg)])
    assert out.exit_code == 1 and "ANTHROPIC_COMPLIANCE_ACCESS_KEY" in out.output
    monkeypatch.setenv("ANTHROPIC_COMPLIANCE_ACCESS_KEY", KEY)
    out = runner.invoke(app, ["import", "compliance", "--config", str(cfg)])
    assert out.exit_code == 0, out.output
    assert "local_sessions: 1 listed, 1 ledgers written, 5 messages" in out.output
    assert KEY not in out.output
    ledger = tmp_path / "runs" / "compliance" / "cowork-clls_01-1.jsonl"
    out = runner.invoke(
        app, ["verify", str(ledger), "--pubkey", str(tmp_path / "keys" / "seatbelt.pub")]
    )
    assert out.exit_code == 0, out.output
    no_block = tmp_path / "n.yaml"
    no_block.write_text("ledgers: runs\nupstreams: {}\n")
    out = runner.invoke(app, ["import", "compliance", "--config", str(no_block)])
    assert out.exit_code == 1 and "compliance:" in out.output


def test_reconstruct_shows_imported_answers_and_their_provenance(tmp_path: Path) -> None:
    fake = FakeCompliance()
    messages = [
        *_transcript(),
        _msg(
            "clsm_x",
            "assistant",
            _text("I did it"),
            model=None,  # the API gives no model for these
            provenance={"type": "client_asserted"},
        ),
        _msg(
            "clsm_y",
            "assistant",
            model=None,
            provenance={"type": "content_unavailable", "reason": "oversize"},
        ),
    ]
    fake.local["s"] = {"meta": _local_meta("s", at(10)), "messages": messages}
    _importer(tmp_path, fake, Clock(120)).run()
    (path,) = (tmp_path / "compliance").glob("*.jsonl")
    console = Console(width=400, record=True)
    timeline(path, console)
    out = console.export_text()
    assert "[marker] [system prompt content not shown]" in out
    assert "<- claude-opus-5-5: I'll read the test file first." in out  # the text, not "? out"
    assert "<- unknown: [unverified] I did it" in out
    assert "<- unknown: [unavailable: oversize]" in out
    assert "(? out)" not in out
