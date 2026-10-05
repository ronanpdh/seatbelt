# Seatbelt: what it is, why, and how to use it

Seatbelt is a model-agnostic harness for AI agents. It records an agent run as a tamper-evident, hash-chained ledger, signs it, attacks the agent with an adversarial corpus, and bundles the results into an evidence pack that anyone can verify offline.

This document is the map. The README is the five-minute tour, the ADRs in `docs/adr/` are the reasoning behind each format, and `docs/spec/` and `docs/schema/` are the normative definitions.

## Why

An agent that calls tools can move money, send mail, delete files. When something goes wrong, three questions follow: who did what, in what order, and can we trust the record that says so.

Framework logs and tracing backends answer the first two only as long as you trust the operator of the log. They do not answer the third. A log line can be edited, a span dropped, a tail truncated, and nothing in the log itself says so.

Seatbelt is built so the record proves itself:

- **Attributable.** Every event names its actor (agent, user, model, tool, policy) and its parent, so lineage is explicit: a response links to its request, a tool result to its call, a policy check to the call it judged, an action to the decision that authorised it.
- **Reconstructable.** One JSONL file per run, one event per line, in order. A timeline can be rebuilt with nothing but the file.
- **Provable.** Each event carries the SHA-256 of the previous one and of its own canonical form. Any edit, reorder, insertion or deletion inside the file breaks the chain at a named sequence number. A signed manifest pins the final hash and file digest, so a re-chained ledger or a truncated tail is caught too, when the signature is checked with the public key.
- **Redacted before it is provable.** Secrets are scrubbed before hashing and writing, so the stored, signed content never contained them.
- **Self-contained.** No database and no network to verify. The process being observed, or the organisation's recording gateway, writes the files, and the reviewer checks them with the CLI and a public key.

The design choices and their trade-offs are recorded in ADR 0001 (ledger), 0002 (attestation), 0003 (scenarios), 0004 (evidence pack), 0005 (sandbox), 0006 (recording gateway) and 0007 (launcher).

## Features

### Ledger

- One `runs/<run id>.jsonl` per run, created mode 0600, one fsync per event, thread-safe appends.
- Event kinds: `run.start`, `user.message`, `model.request`, `model.response`, `tool.call`, `tool.result`, `policy.check`, `decision`, `action`, `outcome`, `run.end`.
- `Event` and `Actor` are strict Pydantic models. An unknown key injected into a line fails to parse rather than silently vanishing from the canonical form.
- Every event carries `schema_version`. Verification rejects versions it does not know. Pre-1.0, a minor release may bump it.
- Attribute names follow the OpenTelemetry GenAI semantic conventions where one exists (`gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.tool.*`); the rest are namespaced (`policy.*`, `decision.*`, `action.*`, `outcome.*`, `sandbox.*`).
- The provider-returned model version is recorded on every response, not just the requested one.

### Redaction

Regex scrubbing of known secret formats (Anthropic, OpenAI, Google and Stripe API keys, seatbelt's own `sbk_` keys, AWS access key ids, GitHub and Slack tokens, bearer tokens, JWTs, private key blocks, passwords in URLs, and `…KEY=`, `…TOKEN=`, `…SECRET=` and `…PASSWORD=` lines as in `.env` files (an upper-case name and a value of 16 characters or more)), applied recursively over dicts (their keys too), lists, tuples and Pydantic models before anything is hashed. `Ledger.append` also redacts every event's actor id, version and parent id. Patterns are anchored to token shapes so ordinary prose survives; a secret in another shape is recorded as it was sent.

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

`seatbelt keygen` writes an Ed25519 key pair (0600, never overwritten). `Recorder.start(..., signer=Signer.from_file(key))` signs a manifest (run id, schema version, event count, final hash, file digest) into `<run id>.attest.json` at run end, failed runs included. `seatbelt attest` signs after the fact and refuses a broken or incomplete chain. `seatbelt verify --pubkey` trusts only the key file you give it, never the key embedded in the manifest, and reports attested, FORGED (fails the key, or without a key a sidecar that contradicts the ledger), UNCHECKED (a sidecar consistent with the ledger, no key) or UNATTESTED (exit 1 when a key was given).

### Timeline reconstruction

`seatbelt reconstruct` verifies first, then prints the run as a Rich table, one row per event. Cells are literal text, never markup, because ledger content is untrusted.

### Scenario pack

A shipped corpus under `scenarios/` of adversarial single-turn scenarios: direct and indirect prompt injection, poisoned context, tool-argument smuggling, credential exfiltration with a canary, code-execution requests, policy trust exploitation, and one benign control. Each scenario is mapped to the OWASP Top 10 for Agentic Applications and, where one exists, a MITRE ATLAS technique.

You supply `target(rec: Recorder, inputs: Inputs)`. Checks (`no_tool_call`, `tool_call`, `policy_denied`, `no_tool_success`, `no_match`, `run_ok`) are evaluated over the ledger, and every finding cites the event ids that prove it. Exit 1 on any finding, 2 on a target that fails to load. Schemas: `docs/schema/scenario.json`, `docs/schema/findings.json`.

### Docker sandbox

`seatbelt scenarios ... --image <img>` runs each scenario in its own hardened container: no network unless the scenario declares `egress: true`, read-only root, no capabilities, host uid, target code mounted read-only. The signing key stays on the host and the CLI refuses a key inside the target directory. Image, image digest and egress are recorded in `run.start`.

### Evidence pack

`seatbelt pack` bundles a runs directory (ledgers, attestation sidecars, `findings.json`, optionally the corpus) into one deterministic zip with a signed `pack.json` manifest. `seatbelt verify-pack` re-checks the signature, every member hash, every chain, every attestation and every finding's evidence, offline. Format: `docs/spec/evidence-pack-v1.md`.

### Recording gateway

`seatbelt gateway serve` is a service the organisation runs so that clients employees already use are recorded with no code change. It speaks the Anthropic Messages API, OpenAI Chat Completions and the OpenAI Responses API, authenticates each employee by an issued key (stored as a hash), swaps in the real provider key, forwards the request body unchanged, relays the response as it arrives, streamed or not, and records the exchange into one signed ledger per employee session. A session closes and is signed after an idle window, or when a named run ends; after a crash, the next start closes and signs every open chain whose hash chain still verifies. An org policy can restrict models, cap output tokens and refuse the results of denied tools. Deployment: `docs/deploy/gateway.md`; Claude Desktop and Cowork: `docs/deploy/claude-desktop-gateway.md`.

Which clients it records:

| Client | How it is pointed at the gateway | Release |
|---|---|---|
| Claude Code | `seatbelt run claude`, or `ANTHROPIC_BASE_URL` | 0.2.0 |
| Claude Desktop, Cowork | MDM or in-app gateway configuration | 0.2.0 |
| Claude SDKs, Claude Agent SDK | base URL | 0.2.0 |
| OpenAI Chat Completions clients (OpenAI SDKs, LangChain) | `OPENAI_BASE_URL` | 0.2.0 |
| Codex | `seatbelt run codex` | 0.3.0 |
| OpenAI Agents SDK, OpenAI SDKs (Responses API) | `OPENAI_BASE_URL` | 0.3.0 |
| Gemini CLI | `seatbelt run gemini` | 0.3.0 |
| Google Gen AI SDKs (Gemini API) | `GOOGLE_GEMINI_BASE_URL` | 0.3.0 |
| claude.ai web, unmanaged desktops (Claude Enterprise) | Compliance API importer, `seatbelt import compliance` | 0.3.0 |

### Launcher

`seatbelt run claude` (or `codex`, or `gemini`) records the CLI. With no gateway configured it records on the machine: the gateway starts inside the run on localhost, the CLI keeps its own sign-in, and the signed ledger lands in the local data folder ([local-recording.md](local-recording.md)). With `gateway` and `key` in `~/.config/seatbelt/config.toml` it starts the CLI pointed at that gateway with the employee's key, strips real provider keys from its environment, and ends the run when it exits.

### Fleet report

`seatbelt report [runs...]` sums one or more runs directories (with none, this machine's runs) by person, model and tool: runs, model calls, tokens, policy refusals, and the failed, open, unsigned, forged and broken runs, and signatures whose ledger is gone. `--people` joins one person's ids across sources. Only chains that verify are counted, and neither forged ledgers nor, with `--pubkey`, ledgers that ended without a signature. A ledger deleted together with its signature leaves nothing for a report to find.

### Supply chain

Tagged releases attach the wheel, sdist, a CycloneDX SBOM and the Sigstore-signed SLSA build provenance (`seatbelt_ai-<version>.intoto.jsonl`) and publish the wheel and sdist to PyPI as `seatbelt-ai` through Trusted Publishing, with PyPI's attestations. The job that builds and attests them installs no dev dependencies and runs no project code; the tests run in a separate job with a read-only token, and the gateway image is pushed only after the reviewer-approved PyPI upload. CI runs ruff, pyright strict and pytest on Python 3.12 and 3.13, CodeQL and Hadolint; a `lowest` job runs the tests and `pip-audit` at every direct dependency's floor, weekly too; uv is pinned to one version, Docker base images and the gateway image's uv are pinned by digest, and the gateway image builds its hash-pinned requirements from `uv.lock`, offline. OpenSSF Scorecard and Best Practices evidence are in the repository.

## How to use it

### Install

```sh
uv tool install seatbelt-ai   # the `seatbelt` command, in its own environment
```

The package is `seatbelt-ai` on PyPI; `seatbelt` there is an unrelated project. To work on seatbelt itself, `git clone https://github.com/ronanpdh/seatbelt && cd seatbelt && uv sync`.

### Record your own agent

Install the library into your agent's environment, with the extra for your SDK:

```sh
pip install "seatbelt-ai[anthropic]"      # or: uv add "seatbelt-ai[openai-agents]"
```

The import name is `seatbelt`, which the unrelated `seatbelt` project on PyPI also uses: install `seatbelt-ai`, never `seatbelt`, and don't install both in one environment. The `uv tool install` above puts the CLI in its own environment, where your code can't import it.

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
uv run seatbelt scenarios scenarios/ --target my_agent:target --image seatbelt-target --target-dir agent/
```

Extend the image with your dependencies (`FROM seatbelt-target`). Keep keys and `.env` files outside `--target-dir`; everything under it is visible to the target, except an `--out` inside it, which is hidden. Point it at the folder with your agent's code (`agent/my_agent.py` here): the default, the current directory, exposes everything in it.

### Run it for a team

```sh
uv sync --extra gateway
uv run seatbelt keygen keys
# write gateway.yaml: signing_key: keys/seatbelt.key, ledgers: runs, upstreams (see docs/deploy/gateway.md)
uv run seatbelt gateway keygen --user alice@corp --config gateway.yaml
uv run seatbelt gateway serve --config gateway.yaml
seatbelt run claude                          # on alice's machine, with gateway and key in ~/.config/seatbelt/config.toml
uv run seatbelt report runs --pubkey keys/seatbelt.pub
```

The config format, a container image, sessions, policy and backups are in `docs/deploy/gateway.md`.

### Hand over the evidence

```sh
uv run seatbelt pack runs --out audit.seatbelt.zip --key keys/seatbelt.key --corpus scenarios/
uv run seatbelt verify-pack audit.seatbelt.zip --pubkey keys/seatbelt.pub
```

The recipient needs the zip, the public key and the harness. Nothing else.

## Command reference

| Command | Does | Exit 1 when |
|---|---|---|
| `seatbelt run <cli> [--config] [--exe] [-- args]` | runs `claude`, `codex` or `gemini`, recorded on this machine or through your gateway | bad config, signing key or spawn error, or unknown CLI (127: executable not found); otherwise the CLI's own exit code, 128 + N when a signal N ended it |
| `seatbelt report [runs...] [--pubkey] [--people] [--json]` | usage by person, model and tool; refused, failed, open and unsigned runs. Takes several folders; `--people` joins one person's ids ([guide](deploy/gateway.md#one-row-per-person)). With no `runs`, this machine's runs | a ledger is broken or forged, a signature's ledger is missing, or with a key given (with no `runs`, this machine's key) one ended unsigned; a bad people file |
| `seatbelt runs [--limit]` | lists this machine's runs by name, newest first | |
| `seatbelt verify [run or ledger] [--pubkey]` | checks the hash chain and attestation; a local run against this machine's key. With no argument, the latest run | broken, forged (a signature that does not match the ledger counts, key or not), incomplete, or unattested with a key given |
| `seatbelt reconstruct [run or ledger] [--pubkey]` | prints the run as a timeline; with no argument, the latest run | same as verify |
| `seatbelt calls [run or ledger] [--pubkey]` | each model call as numbers: messages, tools sent (MCP, deferred), request size, tokens, errors; no prompt text, so it can be shared; with no argument, the latest run | same as verify |
| `seatbelt demo [--out runs]` | records a scripted example run | |
| `seatbelt keygen [dir]` | writes an Ed25519 key pair | a key file exists |
| `seatbelt attest <ledger> --key` | signs a finished ledger into `<id>.attest.json` | broken or incomplete chain, sidecar exists |
| `seatbelt scenarios <corpus> --target m:f [--out] [--key] [--list] [--image] [--target-dir] [--timeout]` | runs the adversarial corpus | any finding (2: bad target) |
| `seatbelt pack <runs> --out <zip> [--key] [--corpus]` | bundles a runs directory into an evidence pack | a broken ledger, a file name that is not a run id, a signature that does not match its ledger (or, with `--key`, was made by another key), output exists |
| `seatbelt verify-pack <zip> [--pubkey]` | re-checks a pack offline | forged, or a broken ledger inside; with a key given, an unsigned pack or a run without its signature |
| `seatbelt erase <runs...> (--principal <id> \| --person <name> --people <file>) --case <ref> [--key \| --config] [--yes]` | removes a person's ledgers inside a signed record; lists only without `--yes` ([guide](deploy/erasure.md)) | a folder in use, a bad people file or config, no signing key; with `--yes`, an unreadable ledger that is or may be the person's |
| `seatbelt gateway keygen --user <id> [--config]` | issues a gateway key; stores only its hash | the user already has a key |
| `seatbelt gateway serve [--config]` | runs the recording gateway | bad config or signing key |
| `seatbelt import compliance [--config]` | imports Claude Enterprise transcripts from Anthropic's Compliance API into signed ledgers ([guide](deploy/compliance-import.md)) | bad config, key or API error, or a conversation that needs a person to check |

The README has the same table; keep the two in step.

## What it does not do

- It does not judge whether an agent's behaviour was correct. It records, checks the record, and evaluates the explicit scenario checks you asked for.
- It does not enforce policy inside a framework's own tool loop. Adapters observe; enforcement is through `Recorder.tool_call`.
- The gateway cannot stop a client running a tool on the employee's machine. It records a denied tool call and refuses to pass the tool's result to the model.
- It does not protect a ledger from a writer who also holds the private key. Attestation proves the file matches what the key holder signed, not that the key holder was honest.
- It is pre-1.0. The ledger schema may change between minor versions; each change bumps `schema_version` and is noted in the changelog.

## Where to read next

- `README.md`: quick start.
- `docs/adr/`: why each format is the way it is.
- `docs/spec/evidence-pack-v1.md`, `docs/schema/`: normative formats.
- `docs/deploy/`: running the gateway, and pointing Claude Desktop at it.
- `docs/openssf-best-practices.md`: supply-chain and process evidence.
- `ROADMAP.md`, `CHANGELOG.md`: what is done and what is next.
