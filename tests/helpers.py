"""Shared test helpers."""

import json
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
