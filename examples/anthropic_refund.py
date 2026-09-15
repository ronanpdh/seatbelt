"""One-tool refund agent against the live Anthropic API, recorded by seatbelt.

Run:
    uv run --extra anthropic python examples/anthropic_refund.py
    uv run seatbelt verify runs/<run id>.jsonl
    uv run seatbelt reconstruct runs/<run id>.jsonl

Credentials come from ANTHROPIC_API_KEY or an `ant auth login` profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anthropic

from seatbelt.adapters.anthropic import AnthropicAdapter
from seatbelt.record.recorder import Recorder

MODEL = "claude-opus-5"
ORDERS = {"1001": {"status": "delivered", "total": 49.0}}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_order",
        "description": "Look up an order's delivery status and total by order id.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    }
]


def lookup_order(order_id: str) -> dict[str, Any]:
    return ORDERS.get(order_id, {"error": f"no order {order_id}"})


def main() -> None:
    client = anthropic.Anthropic()
    with Recorder.start(Path("runs"), agent_id="refund-example", agent_version="0.1") as rec:
        rec.user_message("user-42", "Can I get a refund for order 1001?")
        messages = AnthropicAdapter(rec).messages(client)
        history: list[dict[str, Any]] = [
            {"role": "user", "content": "Can I get a refund for order 1001?"}
        ]
        while True:
            response = messages.create(model=MODEL, max_tokens=16000, tools=TOOLS, messages=history)
            # Send back the full content (thinking blocks included) as plain JSON,
            # so seatbelt's redaction can see into it.
            content = response.model_dump(mode="json")["content"]
            history.append({"role": "assistant", "content": content})
            if response.stop_reason != "tool_use":
                break
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(lookup_order(str(block.input.get("order_id")))),
                }
                for block in response.content
                if block.type == "tool_use"
            ]
            history.append({"role": "user", "content": results})
        answer = "".join(b.text for b in response.content if b.type == "text")
        rec.outcome(answer or f"stopped: {response.stop_reason}", success=bool(answer))
        print(answer)
        print(f"ledger: {rec.ledger.path}")


if __name__ == "__main__":
    main()
