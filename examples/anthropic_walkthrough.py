"""Every Recorder event kind in one live Anthropic run; the token in PROMPT is fake.

Run:
    uv run --extra anthropic python examples/anthropic_walkthrough.py
"""

import json
from pathlib import Path
from typing import Any

import anthropic

from seatbelt.adapters.anthropic import AnthropicAdapter
from seatbelt.record.recorder import Recorder

MODEL = "claude-opus-5"
REFUND_LIMIT = 100.0
ORDERS = {"1001": {"status": "delivered", "total": 49.0}}
PROMPT = (
    "Refund order 1001 please. "
    "My account token is sk-ant-FAKE0000000000000000000000 if you need it."
)
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


def main() -> None:
    client = anthropic.Anthropic()
    with Recorder.start(Path("runs"), agent_id="refund-bot", agent_version="walkthrough") as rec:
        user = rec.user_message("user-42", PROMPT)
        messages = AnthropicAdapter(rec).messages(client)
        history: list[dict[str, Any]] = [{"role": "user", "content": PROMPT}]
        order: dict[str, Any] = {}
        while True:
            response = messages.create(model=MODEL, max_tokens=16000, tools=TOOLS, messages=history)
            history.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break
            results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "tool_use":
                    order = ORDERS.get(str(block.input.get("order_id")), {})
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(order),
                        }
                    )
            history.append({"role": "user", "content": results})

        total = float(order.get("total", 0))
        allowed = 0 < total <= REFUND_LIMIT
        check = rec.policy_check(
            "refund-limit", user.id, allowed, f"{total:.2f} vs {REFUND_LIMIT:.2f} limit"
        )
        if allowed:
            decision = rec.decision(
                f"refund {total:.2f}", authority="agent:auto-under-100", basis=[user.id, check.id]
            )
            rec.action("POST /refunds", target="payments-api", decision_id=decision.id)
        answer = "".join(b.text for b in response.content if b.type == "text")
        rec.outcome(answer or f"stopped: {response.stop_reason}", success=allowed and bool(answer))
        print(answer)
        print(f"ledger: {rec.ledger.path}")


if __name__ == "__main__":
    main()
