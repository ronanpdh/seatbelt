import json
from pathlib import Path
from typing import Any

import pytest

from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import Ledger
from seatbelt.record.recorder import Recorder
from seatbelt.verify.chain import verify_file

anthropic = pytest.importorskip("anthropic")
from anthropic.types import Message  # noqa: E402

from seatbelt.adapters.anthropic import AnthropicAdapter  # noqa: E402

FIXTURE = Path(__file__).parent.parent / "fixtures" / "anthropic_refund.json"


class FakeMessages:
    def __init__(self, replies: list[Message]) -> None:
        self._replies = iter(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Message:
        self.requests.append(kwargs)
        return next(self._replies)


class FakeClient:
    def __init__(self, replies: list[Message]) -> None:
        self.messages = FakeMessages(replies)


def load_replies() -> list[Message]:
    return [Message.model_validate(m) for m in json.loads(FIXTURE.read_text())]


def test_two_turn_tool_loop_is_recorded(tmp_path: Path) -> None:
    client = FakeClient(load_replies())
    with Recorder.start(tmp_path, agent_id="refund-bot", run_id="r1") as rec:
        messages = AnthropicAdapter(rec).messages(client)  # type: ignore[arg-type]
        history: list[dict[str, Any]] = [{"role": "user", "content": "Refund order 1001"}]
        first = messages.create(model="claude-sonnet-4-5", max_tokens=256, messages=history)
        tool_use = next(b for b in first.content if b.type == "tool_use")
        history.append({"role": "assistant", "content": first.model_dump(mode="json")["content"]})
        history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": '{"status": "delivered", "total": 49.0}',
                    }
                ],
            }
        )
        messages.create(model="claude-sonnet-4-5", max_tokens=256, messages=history)

    path = tmp_path / "r1.jsonl"
    assert verify_file(path).ok
    events = list(Ledger(path, "r1").read())
    kinds = [e.kind for e in events]
    assert kinds == [
        Kind.RUN_START,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.RUN_END,
    ]
    response = events[2]
    assert response.actor.version == "claude-sonnet-4-5-20250929"
    assert response.attrs["gen_ai.usage.output_tokens"] == 31
    assert response.parent_id == events[1].id
    call, result = events[3], events[4]
    assert call.attrs["gen_ai.tool.name"] == "lookup_order"
    assert call.attrs["gen_ai.tool.call.id"] == "toolu_01"
    assert result.parent_id == call.id
    assert result.actor.id == "lookup_order"


def test_unmatched_tool_result_is_ignored(tmp_path: Path) -> None:
    client = FakeClient(load_replies()[1:])
    with Recorder.start(tmp_path, agent_id="bot", run_id="r2") as rec:
        messages = AnthropicAdapter(rec).messages(client)  # type: ignore[arg-type]
        messages.create(
            model="m",
            max_tokens=10,
            messages=[
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "nope"}]}
            ],
        )
    kinds = [e.kind for e in Ledger(tmp_path / "r2.jsonl", "r2").read()]
    assert Kind.TOOL_RESULT not in kinds
