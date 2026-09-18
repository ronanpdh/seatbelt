# Seatbelt: what it is, why, and how to use it

Seatbelt is a model-agnostic harness for AI agents. It records an agent run as a tamper-evident, hash-chained ledger, signs it, attacks the agent with an adversarial corpus, and bundles the results into an evidence pack that anyone can verify offline.

This document is the map. The README is the five-minute tour, the ADRs in `docs/adr/` are the reasoning behind each format, and `docs/spec/` and `docs/schema/` are the normative definitions.

## Why

An agent that calls tools can move money, send mail, delete files. When something goes wrong, three questions follow: who did what, in what order, and can we trust the record that says so.

Framework logs and tracing backends answer the first two only as long as you trust the operator of the log. They do not answer the third. A log line can be edited, a span dropped, a tail truncated, and nothing in the log itself says so.

Seatbelt is built so the record proves itself:

- **Attributable.** Every event names its actor (agent, user, model, tool, policy) and its parent, so lineage is explicit: a response links to its request, a tool result to its call, a policy check to the call it judged, an action to the decision that authorised it.
- **Reconstructable.** One JSONL file per run, one event per line, in order. A timeline can be rebuilt with nothing but the file.
- **Provable.** Each event carries the SHA-256 of the previous one and of its own canonical form. Any edit, reorder, insertion or deletion inside the file breaks the chain at a named sequence number. A signed manifest pins the final hash and file digest, so a rewritten or truncated tail is caught too.
- **Redacted before it is provable.** Secrets are scrubbed before hashing and writing, so the stored, signed content never contained them.
- **Self-contained.** No database, no service, no network. The process being observed writes to its own disk, and the reviewer checks the file with the CLI and a public key.

The design choices and their trade-offs are recorded in ADR 0001 (ledger), 0002 (attestation), 0003 (scenarios), 0004 (evidence pack) and 0005 (sandbox).

## Features

### Ledger

- One `runs/<run id>.jsonl` per run, created mode 0600, one fsync per event, thread-safe appends.
- Event kinds: `run.start`, `user.message`, `model.request`, `model.response`, `tool.call`, `tool.result`, `policy.check`, `decision`, `action`, `outcome`, `run.end`.
- `Event` and `Actor` are strict Pydantic models. An unknown key injected into a line fails to parse rather than silently vanishing from the canonical form.
- Every event carries `schema_version`. Verification rejects versions it does not know. Pre-1.0, a minor release may bump it.
- Attribute names follow the OpenTelemetry GenAI semantic conventions where one exists (`gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.tool.*`); the rest are namespaced (`policy.*`, `decision.*`, `action.*`, `outcome.*`, `sandbox.*`).
- The provider-returned model version is recorded on every response, not just the requested one.

### Redaction

Regex scrubbing of Anthropic and OpenAI keys, AWS access keys, bearer tokens and GitHub tokens, applied recursively over dicts, lists, tuples and Pydantic models before anything is hashed. Patterns are anchored to token shapes so ordinary prose survives.

### Recorder

`seatbelt.record.recorder.Recorder` is the only API adapters and your own code call. `Recorder.start` is a context manager that writes `run.start` and `run.end` (with `ok=False` and the error on exception), refuses to append to an existing ledger, and rejects a run id that is not a safe filename. `model_call` and `tool_call` are context managers that record an error response or result if the body raises, so a crash mid-call still leaves a closed, verifiable record.

### Policy engine

`Recorder.start(..., policy=Policy(*rules))` evaluates every rule against each `Recorder.tool_call` before the tool body runs, records one `policy.check` per rule linked to the `tool.call`, and raises `PolicyDenied` if any rule denies. A rule that raises counts as a denial. Built-in rules: `allowlist(*tools)`, `denylist(*tools)`. A custom rule is a name and a callable `(tool, arguments) -> reason or None`.

The adapters only observe tool calls the framework dispatches, so they record no checks. Enforcement is through `Recorder.tool_call`.

### Adapters

- **Anthropic Messages API.** `AnthropicAdapter(rec).messages(client)` wraps `create` and `stream`, sync or async, including `client.beta`. `tool_use` blocks in a response become `tool.call` events; matching `tool_result` blocks in the next request become `tool.result` events. An abandoned stream is recorded as what was received.
- **OpenAI Agents SDK.** `agents.add_trace_processor(SeatbeltProcessor(rec))` turns generation and response spans into model events, function spans into tool events, and agent, handoff and guardrail spans into `decision` events with the agent as authority.

Adapters translate. They never interpret.

### Verification

`seatbelt verify` checks sequence order, `prev_hash` links and content hashes, and reports the first bad seq. It separately checks completeness: exactly one `run.start` at seq 0 and a final `run.end` whose event count matches. Exit 1 on BROKEN, INCOMPLETE, corrupt or missing.

### Attestation

`seatbelt keygen` writes an Ed25519 key pair (0600, never overwritten). `Recorder.start(..., signer=Signer.from_file(key))` signs a manifest (run id, schema version, event count, final hash, file digest) into `<run id>.attest.json` at run end, failed runs included. `seatbelt attest` signs after the fact and refuses a broken or incomplete chain. `seatbelt verify --pubkey` trusts only the key file you give it, never the key embedded in the manifest, and reports attested, FORGED, UNCHECKED (sidecar, no key) or UNATTESTED (exit 1 when a key was given).

### Timeline reconstruction

`seatbelt reconstruct` verifies first, then prints the run as a Rich table, one row per event. Cells are literal text, never markup, because ledger content is untrusted.

### Scenario pack

A shipped corpus under `scenarios/` of adversarial single-turn scenarios: direct and indirect prompt injection, poisoned context, tool-argument smuggling, credential exfiltration with a canary, code-execution requests, policy trust exploitation, and one benign control. Each scenario is mapped to the OWASP Top 10 for Agentic Applications and, where one exists, a MITRE ATLAS technique.

You supply `target(rec: Recorder, inputs: Inputs)`. Checks (`no_tool_call`, `tool_call`, `policy_denied`, `no_tool_success`, `no_match`, `run_ok`) are evaluated over the ledger, and every finding cites the event ids that prove it. Exit 1 on any finding, 2 on a target that fails to load. Schemas: `docs/schema/scenario.json`, `docs/schema/findings.json`.

### Docker sandbox

`seatbelt scenarios ... --image <img>` runs each scenario in its own hardened container: no network unless the scenario declares `egress: true`, read-only root, no capabilities, host uid, target code mounted read-only. The signing key stays on the host and the CLI refuses a key inside the target directory. Image, image digest and egress are recorded in `run.start`.

### Evidence pack

`seatbelt pack` bundles a runs directory (ledgers, attestation sidecars, `findings.json`, optionally the corpus) into one deterministic zip with a signed `pack.json` manifest. `seatbelt verify-pack` re-checks the signature, every member hash, every chain, every attestation and every finding's evidence, offline. Format: `docs/spec/evidence-pack-v1.md`.

### Supply chain

Tagged releases attach the wheel, sdist and a CycloneDX SBOM with Sigstore-signed SLSA build provenance. CI runs ruff, pyright strict and pytest on Python 3.12 and 3.13. OpenSSF Scorecard and Best Practices evidence are in the repository.

## How to use it

### Install

```sh
git clone https://github.com/ronanpdh/seatbelt && cd seatbelt
uv sync
```

### Record your own agent

```python
from pathlib import Path
from seatbelt.record.recorder import Recorder
from seatbelt.policy.engine import Policy, PolicyDenied, allowlist
from seatbelt.attest.sign import Signer

policy = Policy(allowlist("lookup_order", "issue_refund"))
signer = Signer.from_file(Path("keys/seatbelt.key"))

with Recorder.start(Path("runs"), agent_id="support-bot", policy=policy, signer=signer) as rec:
    rec.user_message("u-42", "Refund order 1001")
    with rec.model_call("claude-sonnet-5", {"messages": [...]}) as call:
        call.respond(
            {"content": "..."},
            usage={"input_tokens": 12},
            response_model="claude-sonnet-5-20260601",
        )
    try:
        with rec.tool_call("issue_refund", {"order": 1001, "amount": 20}) as tool:
            tool.result({"refund_id": "r-1"})
    except PolicyDenied as denied:
        rec.decision("refund refused", authority="policy", basis=[denied.call.id])
    rec.outcome("refund issued", success=True)
```

Every write goes through the recorder, which redacts, hashes and appends. When the block exits, `run.end` is written and the ledger is signed.

### Record a framework agent

Anthropic SDK:

```python
from seatbelt.adapters.anthropic import AnthropicAdapter

messages = AnthropicAdapter(rec).messages(
    client
)  # or client.beta, or .async_messages(async_client)
messages.create(model=..., messages=..., tools=...)
```

OpenAI Agents SDK:

```python
import agents
from seatbelt.adapters.openai_agents import SeatbeltProcessor

agents.add_trace_processor(SeatbeltProcessor(rec))
```

Working examples: `examples/anthropic_refund.py`, `examples/anthropic_walkthrough.py`, `examples/openai_agents_refund.py`.

### Verify and replay

```sh
uv run seatbelt keygen keys
uv run seatbelt verify runs/<run id>.jsonl --pubkey keys/seatbelt.pub
uv run seatbelt reconstruct runs/<run id>.jsonl --pubkey keys/seatbelt.pub
```

Sign after the fact with `seatbelt attest <ledger> --key keys/seatbelt.key`. Prove the chain catches tampering: edit any line of the ledger and run `verify` again.

### Attack your agent

Write `target(rec, inputs)` that drives your agent with the recorder and routes tool results through `inputs.tool_result(name, real)` so poisoned results reach it. The corpus uses a reference set of names (`lookup_order`, `issue_refund`, a policy named `refund-limit`, the canary `CANARY-7F3A9`); copy a scenario and change the names for your agent. A target must end cleanly to pass, so catch `PolicyDenied` and refuse gracefully.

```sh
uv run seatbelt scenarios scenarios/ --list
uv run seatbelt scenarios scenarios/ --target my_agent:target --key keys/seatbelt.key --out runs
```

Sandboxed:

```sh
rm -rf dist && uv build && docker build -f docker/Dockerfile -t seatbelt-target .
uv run seatbelt scenarios scenarios/ --target my_agent:target --image seatbelt-target --target-dir .
```

Extend the image with your dependencies (`FROM seatbelt-target`). Keep keys, `.env` and `runs/` outside `--target-dir`; everything under it is visible to the target.

### Hand over the evidence

```sh
uv run seatbelt pack runs --out audit.seatbelt.zip --key keys/seatbelt.key --corpus scenarios/
uv run seatbelt verify-pack audit.seatbelt.zip --pubkey keys/seatbelt.pub
```

The recipient needs the zip, the public key and the harness. Nothing else.

## Command reference

| Command | Does | Exit 1 when |
|---|---|---|
| `seatbelt demo [--out runs]` | records a scripted example run | |
| `seatbelt verify <ledger> [--pubkey]` | checks chain and attestation | broken, forged, incomplete, or unattested with a key given |
| `seatbelt reconstruct <ledger> [--pubkey]` | prints the run as a timeline | same as verify |
| `seatbelt keygen [dir]` | writes an Ed25519 key pair | a key file exists |
| `seatbelt attest <ledger> --key` | signs a finished ledger | broken or incomplete chain, sidecar exists |
| `seatbelt scenarios <corpus> --target m:f [--out] [--key] [--list] [--image] [--target-dir] [--timeout]` | runs the adversarial corpus | any finding (2: bad target) |
| `seatbelt pack <runs> --out <zip> [--key] [--corpus]` | builds an evidence pack | broken ledger, output exists |
| `seatbelt verify-pack <zip> [--pubkey]` | re-checks a pack offline | forged, or a broken ledger inside |

## What it does not do

- It does not judge whether an agent's behaviour was correct. It records, checks the record, and evaluates the explicit scenario checks you asked for.
- It does not enforce policy inside a framework's own tool loop. Adapters observe; enforcement is through `Recorder.tool_call`.
- It does not protect a ledger from a writer who also holds the private key. Attestation proves the file matches what the key holder signed, not that the key holder was honest.
- It is pre-1.0. The ledger schema may change between minor versions; each change bumps `schema_version` and is noted in the changelog.

## Where to read next

- `README.md`: quick start.
- `docs/adr/`: why each format is the way it is.
- `docs/spec/evidence-pack-v1.md`, `docs/schema/`: normative formats.
- `docs/openssf-best-practices.md`: supply-chain and process evidence.
- `ROADMAP.md`, `CHANGELOG.md`: what is done and what is next.
