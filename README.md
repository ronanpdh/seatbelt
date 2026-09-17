<img src="docs/assets/seatbelt-cover.png" alt="Seatbelt" width="100%">

# seatbelt

Seatbelt is a model-agnostic harness for AI agents. It records what an agent did as a tamper-evident ledger you can verify and replay as a timeline.

**What it records.** Every step of an agent run: user messages, model requests and responses (with the exact model version and token usage), tool calls and results, policy checks, decisions with their authority and basis, actions and outcomes.

**What it produces.** One JSONL file per run. Each event is SHA-256 hash-chained to the one before it, and secrets are redacted before anything is hashed or written. Any edit, reorder or deletion inside the chain makes `seatbelt verify` fail and name the first bad event. A signed manifest beside it pins the final hash, so a rewritten tail fails too.

**Try it in five minutes.**

```sh
git clone https://github.com/ronanpdh/seatbelt && cd seatbelt
uv sync
uv run seatbelt demo                        # writes runs/<run id>.jsonl
uv run seatbelt verify runs/<run id>.jsonl
uv run seatbelt reconstruct runs/<run id>.jsonl
```

**Prove the tail too.** The chain catches edits inside the file; a signed manifest catches a rewritten ending.

```sh
uv run seatbelt keygen keys                                  # seatbelt.key (private), seatbelt.pub, both 0600
uv run seatbelt attest runs/<run id>.jsonl --key keys/seatbelt.key
uv run seatbelt verify runs/<run id>.jsonl --pubkey keys/seatbelt.pub   # attested, or FORGED / UNATTESTED
```

Or sign at run end: `Recorder.start(..., signer=Signer.from_file(Path("keys/seatbelt.key")))` (`from seatbelt.attest.sign import Signer`).

**Attack it.** The shipped corpus feeds prompt injection, tool-argument smuggling, credential exfiltration and more to your agent, then checks the ledger.

```sh
uv run seatbelt scenarios scenarios/ --list
uv run seatbelt scenarios scenarios/ --target examples.scenario_target:target   # one deliberate FAIL
```

Your target is a function `target(rec: Recorder, inputs: Inputs)` that drives your agent with the recorder; call `inputs.tool_result(name, real)` where your tools return so poisoned results reach the agent. The corpus names a reference set: the tool names, a policy named `refund-limit` (`trust-exploitation-policy` expects it to deny) and the string `CANARY-7F3A9`, which `canary-exfiltration` expects planted wherever your agent keeps its credential; copy a scenario and change the names for yours. `no_match` runs on the redacted ledger, so a canary shaped like a real key (`sk-ant-...`, `AKIA...`) is scrubbed before the check sees it; use an inert string. A target must end cleanly to pass, so catch `PolicyDenied` (from `seatbelt.policy.engine`) and refuse gracefully rather than let it propagate. Every failure cites ledger event ids.

**Hand it over.** One zip, one command to check it.

```sh
uv run seatbelt pack runs --out audit.seatbelt.zip --key keys/seatbelt.key --corpus scenarios/
uv run seatbelt verify-pack audit.seatbelt.zip --pubkey keys/seatbelt.pub
```

The pack carries every ledger, its attestation, the findings and the corpus, bound by a signed manifest. Format: [`docs/spec/evidence-pack-v1.md`](docs/spec/evidence-pack-v1.md).

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
