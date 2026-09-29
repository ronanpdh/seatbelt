"""Regressions for where the review's fixes meet: found by checking the merged change."""
# pyright: reportPrivateUsage=false

from pathlib import Path
from typing import Any

from tests.unit.test_review_formats_sink import FakeStore, _closed_ledger

from seatbelt.gateway import local
from seatbelt.gateway.formats.gemini import GeminiFormat
from seatbelt.gateway.sink import Sink
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
from seatbelt.locks import RUNNING
from seatbelt.record.recorder import Recorder


def _gemini_result_recorded(tmp_path: Path, arg: str) -> bool:
    """A call the API gave no id, answered under the id Gemini CLI minted for it."""
    with Recorder.start(tmp_path, "agent") as rec:
        fmt = GeminiFormat(rec)
        ask = {"role": "user", "parts": [{"text": "go"}]}
        call = fmt.begin({"model": "gemini-2.5-pro", "contents": [ask]})
        part = {"functionCall": {"name": "shell", "args": {"cmd": arg}}}
        content = {"role": "model", "parts": [part]}
        fmt.finish(call, {"candidates": [{"content": content, "finishReason": "STOP"}]})
        answered = {"id": "cli-1", "name": "shell", "args": {"cmd": arg}}
        result = {"id": "cli-1", "name": "shell", "response": {"output": "ok"}}
        contents: list[dict[str, Any]] = [
            ask,
            {"role": "model", "parts": [{"functionCall": answered}]},
            {"role": "user", "parts": [{"functionResponse": result}]},
        ]
        fmt.begin({"model": "gemini-2.5-pro", "contents": contents})
    return Kind.TOOL_RESULT in [e.kind for e in read_events(next(tmp_path.glob("*.jsonl")))]


def test_a_gemini_result_is_matched_when_the_call_held_a_lone_surrogate(tmp_path: Path) -> None:
    # the ledger writes the surrogate as the text \ud800; matching compares the same form
    assert _gemini_result_recorded(tmp_path, "echo \ud800")


def test_a_run_name_that_is_a_path_is_never_used_as_one(tmp_path: Path) -> None:
    ledgers = tmp_path / "runs"
    (ledgers / RUNNING).mkdir(parents=True)
    outside = tmp_path / "outside.lock"
    outside.write_text("")
    path = ledgers / "odd.jsonl"
    ledger = Ledger(path, "odd")
    ledger.append(
        Kind.RUN_START, Actor(type=ActorType.AGENT, id="x"), {"run.name": "../../outside"}
    )
    signer = local.local_signer(tmp_path / "keys")
    assert local.close_dead_runs(ledgers, signer) == []  # not a local run's name: left alone
    assert outside.exists()
    assert list(read_events(path))[-1].kind is Kind.RUN_START


def test_the_next_ledger_ships_as_soon_as_a_refused_one_goes_to_the_back(tmp_path: Path) -> None:
    bad = _closed_ledger(tmp_path, "bad", None)
    good = _closed_ledger(tmp_path, "good", None)
    store = FakeStore(refuse="bad")
    sink = Sink(store, tmp_path, backoff=(0.01, 60.0), attempts=2)  # a long wait, not taken
    sink.start()
    sink.ship(bad)
    sink.ship(good)
    assert store.done.wait(5)
    sink.stop(timeout=0.5)
    assert sink.shipped(good)
