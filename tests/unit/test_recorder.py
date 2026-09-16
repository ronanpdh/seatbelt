from pathlib import Path

import pytest

from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder
from seatbelt.verify.chain import verify_file


def test_recorder_writes_a_verifiable_run(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="bot", run_id="run1") as rec:
        rec.user_message("u1", "hello sk-ant-abcdefghijklmnopqrstuvwxyz1234")
        with rec.model_call("m", {"messages": []}) as call:
            resp = call.respond(
                {"content": "hi"}, usage={"output_tokens": 1}, response_model="m-2026"
            )
        with rec.tool_call("echo", {"x": 1}) as tool:
            tool.result({"x": 1})
        rec.outcome("done", success=True)

    path = tmp_path / "run1.jsonl"
    events = list(read_events(path))
    kinds = [e.kind for e in events]
    assert kinds[0] == Kind.RUN_START
    assert kinds[-1] == Kind.RUN_END
    assert Kind.MODEL_RESPONSE in kinds
    assert verify_file(path).ok
    # attribution and lineage
    assert resp.parent_id == events[2].id
    assert events[3].actor.version == "m-2026"
    # redaction happened before hashing
    assert "sk-ant-" not in path.read_text()


def test_exception_is_recorded_as_failed_run(tmp_path: Path) -> None:
    try:
        with Recorder.start(tmp_path, agent_id="bot", run_id="r2"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    events = list(read_events(tmp_path / "r2.jsonl"))
    assert events[-1].kind == Kind.RUN_END
    assert events[-1].attrs["run.ok"] is False


def test_exception_reason_is_recorded_on_run_end(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), Recorder.start(tmp_path, agent_id="bot", run_id="r3"):
        raise RuntimeError("boom sk-ant-abcdefghijklmnopqrstuvwxyz1234")
    end = list(read_events(tmp_path / "r3.jsonl"))[-1]
    assert end.attrs["run.ok"] is False
    assert end.attrs["run.error"] == "RuntimeError: boom [REDACTED:anthropic_key]"


def test_failed_model_call_records_an_error_response(tmp_path: Path) -> None:
    with (
        pytest.raises(TimeoutError),
        Recorder.start(tmp_path, agent_id="bot", run_id="r4") as rec,
        rec.model_call("m", {"messages": []}),
    ):
        raise TimeoutError("upstream")
    request, response = list(read_events(tmp_path / "r4.jsonl"))[1:3]
    assert response.kind == Kind.MODEL_RESPONSE
    assert response.parent_id == request.id
    assert response.attrs["error"] == "TimeoutError: upstream"


def test_answered_model_call_that_then_fails_is_not_answered_twice(tmp_path: Path) -> None:
    with (
        pytest.raises(ValueError),
        Recorder.start(tmp_path, agent_id="bot", run_id="r5") as rec,
        rec.model_call("m", {"messages": []}) as call,
    ):
        call.respond({"content": "hi"})
        raise ValueError("after")
    kinds = [e.kind for e in read_events(tmp_path / "r5.jsonl")]
    assert kinds.count(Kind.MODEL_RESPONSE) == 1


def test_failed_tool_call_records_an_error_result(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError),
        Recorder.start(tmp_path, agent_id="bot", run_id="r6") as rec,
        rec.tool_call("pay", {}),
    ):
        raise RuntimeError("exploded")
    call, result = list(read_events(tmp_path / "r6.jsonl"))[1:3]
    assert result.kind == Kind.TOOL_RESULT
    assert result.parent_id == call.id
    assert result.attrs["error"] == "RuntimeError: exploded"
