"""One-tool refund agent on the OpenAI Agents SDK, recorded by seatbelt.

Run:
    uv run --extra openai-agents python examples/openai_agents_refund.py
    uv run seatbelt reconstruct runs/<run id>.jsonl

Credentials come from OPENAI_API_KEY. Compare with examples/anthropic_refund.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents import Agent, Runner, add_trace_processor, function_tool

from seatbelt.adapters.openai_agents import SeatbeltProcessor
from seatbelt.record.recorder import Recorder

ORDERS = {"1001": {"status": "delivered", "total": 49.0}}


@function_tool
def lookup_order(order_id: str) -> str:
    """Look up an order's delivery status and total by order id."""
    return json.dumps(ORDERS.get(order_id, {"error": f"no order {order_id}"}))


def main() -> None:
    agent = Agent(name="refund-example", tools=[lookup_order])
    with Recorder.start(Path("runs"), agent_id="refund-example", agent_version="0.1") as rec:
        add_trace_processor(SeatbeltProcessor(rec))
        rec.user_message("user-42", "Can I get a refund for order 1001?")
        result = Runner.run_sync(agent, "Can I get a refund for order 1001?")
        answer = str(result.final_output)
        rec.outcome(answer, success=bool(answer))
        print(answer)
        print(f"ledger: {rec.ledger.path}")


if __name__ == "__main__":
    main()
