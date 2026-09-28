"""Coverage-guided fuzzing of the code that reads untrusted bytes: ledger files handed to
`seatbelt verify`, and the provider streams the gateway reassembles.

Run: uv run --group fuzz python fuzz/fuzz_parsers.py -max_total_time=60 [corpus_dir]
Each target states what it may raise; anything else is a bug."""

import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports():
    from seatbelt.gateway.app import sse_events
    from seatbelt.gateway.formats import anthropic, openai_chat, openai_responses
    from seatbelt.ledger.store import LedgerError, read_events
    from seatbelt.verify.chain import verify_events

_DIR = Path(tempfile.mkdtemp(prefix="seatbelt-fuzz-"))
_LEDGER = _DIR / "run.jsonl"


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
    ):
        assemble(events)


def test_one_input(data: bytes) -> None:
    if not data:
        return
    (ledger if data[0] % 2 else stream)(data[1:])


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
