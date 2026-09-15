<img src="docs/assets/seatbelt-cover.png" alt="Seatbelt" width="100%">

# seatbelt

Seatbelt is a model-agnostic harness for AI agents. It records what an agent did as a tamper-evident ledger you can verify and replay as a timeline.

**What it records.** Every step of an agent run: user messages, model requests and responses (with the exact model version and token usage), tool calls and results, policy checks, decisions with their authority and basis, actions and outcomes.

**What it produces.** One JSONL file per run. Each event is SHA-256 hash-chained to the one before it, and secrets are redacted before anything is hashed or written. Any edit, reorder or deletion inside the chain makes `seatbelt verify` fail and name the first bad event.

**Try it in five minutes.**

```sh
git clone https://github.com/ronanpdh/seatbelt && cd seatbelt
uv sync
uv run seatbelt demo                        # writes runs/<run id>.jsonl
uv run seatbelt verify runs/<run id>.jsonl
uv run seatbelt reconstruct runs/<run id>.jsonl
```

## Recording your own agent

```python
from pathlib import Path
from seatbelt.record.recorder import Recorder

with Recorder.start(Path("runs"), agent_id="support-bot") as rec:
    rec.user_message("u-42", "Refund order 1001")
    with rec.tool_call("lookup_order", {"order": 1001}) as tool:
        tool.result({"status": "delivered"})
    rec.outcome("refund issued", success=True)
```

Using the Anthropic SDK? Wrap the client and every `create` or `stream` call is recorded, tool calls included: `AnthropicAdapter(rec).messages(client)`, `.messages(client.beta)`, or `.async_messages(async_client)`. See [`examples/anthropic_refund.py`](examples/anthropic_refund.py).

Using the OpenAI Agents SDK? Register `agents.add_trace_processor(SeatbeltProcessor(rec))` once at startup. See [`examples/openai_agents_refund.py`](examples/openai_agents_refund.py).

## Status

Pre-1.0 and moving quickly: the ledger schema may change between minor versions. See [ROADMAP.md](ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md).

## Project

- [CONTRIBUTING.md](CONTRIBUTING.md) and [STANDARDS.md](STANDARDS.md)
- [SECURITY.md](SECURITY.md) for reporting vulnerabilities
- [Code of conduct](CODE_OF_CONDUCT.md)
- Licensed under [Apache-2.0](LICENSE)
