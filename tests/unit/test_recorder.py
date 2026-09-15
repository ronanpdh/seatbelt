from pathlib import Path

from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import Ledger
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
    events = list(Ledger(path, "run1").read())
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
    events = list(Ledger(tmp_path / "r2.jsonl", "r2").read())
    assert events[-1].kind == Kind.RUN_END
    assert events[-1].attrs["run.ok"] is False
