"""Shared test helpers."""

import json
import sys
from pathlib import Path
from typing import Any


def sse(message: dict[str, Any]) -> bytes:
    def event(name: str, data: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"

    start: dict[str, Any] = {**message, "content": [], "stop_reason": None}
    out = [event("message_start", {"message": start})]
    for i, block in enumerate(message["content"]):
        if block["type"] == "text":
            out.append(
                event("content_block_start", {"index": i, "content_block": {**block, "text": ""}})
            )
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            out.append(
                event("content_block_start", {"index": i, "content_block": {**block, "input": {}}})
            )
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        out.append(event("content_block_delta", {"index": i, "delta": delta}))
        out.append(event("content_block_stop", {"index": i}))
    usage = {"output_tokens": message["usage"]["output_tokens"]}
    out.append(
        event("message_delta", {"delta": {"stop_reason": message["stop_reason"]}, "usage": usage})
    )
    out.append(event("message_stop", {}))
    return "".join(out).encode()


# Claude Code as tests run it: Python, after the `--settings` that `seatbelt run` puts first,
# which it keeps in SEATBELT_TEST_SETTINGS for the test to read
_FAKE_CLAUDE = """#!{python}
import os, sys
args = sys.argv[1:]
if args[:1] == ["--settings"]:
    os.environ["SEATBELT_TEST_SETTINGS"] = args[1]
    args = args[2:]
os.execv(sys.executable, [sys.executable, *args])
"""


def fake_claude(directory: Path) -> str:
    """An executable that stands in for `claude`: it runs Python with what follows the
    `--settings` seatbelt gives Claude Code."""
    path = directory / "fake-claude"
    path.write_text(_FAKE_CLAUDE.format(python=sys.executable))
    path.chmod(0o755)
    return str(path)


def no_gateway_policy(url: str, key: str) -> None:
    """A preflight that finds a gateway too old to say its policy: the run goes on."""
    return None
