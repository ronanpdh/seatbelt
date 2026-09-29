"""Coverage-guided fuzzing of the code that reads untrusted bytes: ledger files handed to
`seatbelt verify`, the provider streams the gateway reassembles, and the request and
response bodies each wire format checks and records.

Run: uv run --group fuzz python fuzz/fuzz_parsers.py -max_total_time=60 [corpus_dir]
Each target states what it may raise; anything else is a bug."""

import json
import sys
import tempfile
import traceback
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import atheris

with atheris.instrument_imports():
    from seatbelt.gateway.app import sse_events
    from seatbelt.gateway.formats import anthropic, gemini, openai_chat, openai_responses
    from seatbelt.ledger.store import LedgerError, read_events
    from seatbelt.record.recorder import Recorder
    from seatbelt.verify.chain import verify_events

_DIR = Path(tempfile.mkdtemp(prefix="seatbelt-fuzz-"))
_LEDGER = _DIR / "run.jsonl"
_REC = ExitStack().enter_context(Recorder.start(_DIR / "rec", agent_id="fuzz", run_id="fuzz"))
_FORMATS = (
    anthropic.AnthropicFormat,
    openai_chat.OpenAIChatFormat,
    openai_responses.OpenAIResponsesFormat,
    gemini.GeminiFormat,
    gemini.CodeAssistFormat,
)


def ledger(data: bytes) -> None:
    """A ledger file: reading may refuse it, verifying must return a verdict."""
    _LEDGER.write_bytes(data)
    try:
        events = list(read_events(_LEDGER))
    except (LedgerError, UnicodeDecodeError):
        return
    verify_events(events)


def stream(data: bytes) -> None:
    """An upstream SSE body: parsing and every assembler must never raise, since the gateway
    records a broken stream from a `finally`."""
    events = sse_events(data)
    for assemble in (
        anthropic.assemble_sse,
        openai_chat.assemble_sse,
        openai_responses.assemble_sse,
        gemini.assemble_sse,
    ):
        assemble(events)


def _refused_by_ledger(exc: ValueError) -> bool:
    """The ledger refuses to write what it cannot hash (a lone surrogate, nesting deeper
    than it serialises); the gateway then answers 500 and forwards nothing."""
    return any(
        f.filename.endswith("ledger/store.py") for f in traceback.extract_tb(exc.__traceback__)
    )


def body(data: bytes) -> None:
    """A request or response body: every format's checks, and its recording of the body as a
    request and as a response, must take any JSON object."""
    try:
        value: Any = json.loads(data)
    except (ValueError, RecursionError):
        return
    if not isinstance(value, dict):
        return
    for fmt in _FORMATS:
        fmt.unrecordable(value)
        fmt.tool_result_calls(value)
        recorder = fmt(_REC)
        try:
            recorder.finish(recorder.begin(value), value)
        except ValueError as exc:
            if not _refused_by_ledger(exc):
                raise


def test_one_input(data: bytes) -> None:
    if not data:
        return
    (stream, ledger, body)[data[0] % 3](data[1:])


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
