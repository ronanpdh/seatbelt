<img src="docs/assets/seatbelt-cover.png" alt="Seatbelt" width="100%">

# seatbelt

[![ci](https://github.com/ronanpdh/seatbelt/actions/workflows/ci.yml/badge.svg)](https://github.com/ronanpdh/seatbelt/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/ronanpdh/seatbelt/badge)](https://scorecard.dev/viewer/?uri=github.com/ronanpdh/seatbelt)

Seatbelt records what your AI agents do: every prompt, model response and tool call. The record is tamper-evident and signed, so you can prove later exactly what happened.

## Quick start

```sh
uv tool install git+https://github.com/ronanpdh/seatbelt   # needs uv: https://docs.astral.sh/uv/
seatbelt run claude                                         # or: seatbelt run codex, seatbelt run gemini
```

Use the CLI as you normally would; it keeps its own sign-in, whether that's a subscription or an API key. When you exit, seatbelt prints the run's name (e.g. `claude-99ce72ff`) and saves a signed record of it. Then:

```sh
seatbelt reconstruct                 # replay your latest run as a timeline
seatbelt reconstruct claude-99ce72ff # or a run by name
seatbelt runs                        # list your runs by name
seatbelt report                      # what your runs did: models, tools, tokens, anything refused or altered
```

There is nothing to set up. How it works, where runs are saved, and what is and isn't recorded: [docs/local-recording.md](docs/local-recording.md).

No agent to hand? `seatbelt demo` records an example run, then `seatbelt verify runs/<run id>.jsonl` checks it. `verify` and `reconstruct` take a file path, or a run's name from `seatbelt runs`.

## What you get

- **One file per run** (JSONL): user messages, model requests and responses (with the exact model version and token usage), tool calls and results, and policy checks.
- **Tamper evidence.** Each event is SHA-256 hash-chained to the one before it. Any edit, reorder or deletion makes `seatbelt verify` fail and name the first bad event.
- **A signature.** A signed manifest pins the final hash, so a rewritten ending fails too.
- **Redaction.** Secrets are removed before anything is hashed or written.

## Record for a team

To record everyone's agents in one place, run a gateway ([docs/deploy/gateway.md](docs/deploy/gateway.md)). Each person then adds two lines to `~/.config/seatbelt/config.toml`, and `seatbelt run` records through the gateway instead:

```toml
gateway = "https://gw.corp.example"
key = "sbk_..."
```

The gateway holds the provider keys and signs one ledger per session. It can also:
- restrict models, output tokens and tools;
- accept Claude Desktop users signed in with your identity provider;
- ship every ledger to object storage.

`seatbelt report runs --pubkey keys/seatbelt.pub` summarises who used what.

## Record your own agent

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

To sign each run when it ends, make a key with `seatbelt keygen keys` and pass `signer=Signer.from_file(Path("keys/seatbelt.key"))` to `Recorder.start` (`from seatbelt.attest.sign import Signer`).

## Test your agent

The shipped corpus feeds prompt injection, tool-argument smuggling, credential exfiltration and more to your agent, then checks the ledger. It can also run your agent in a sandbox with no network, and bundle the results into one signed evidence pack. See [docs/scenarios.md](docs/scenarios.md).

```sh
git clone https://github.com/ronanpdh/seatbelt && cd seatbelt && uv sync
uv run seatbelt scenarios scenarios/ --target examples.scenario_target:target   # one deliberate FAIL
```

## Commands

| Command | Does | Exit 1 when |
|---|---|---|
| `seatbelt run <cli> [--config] [--exe] [-- args]` | runs `claude`, `codex` or `gemini`, recorded on this machine or through your gateway | bad config or unknown CLI (127: executable not found); otherwise the CLI's own exit code |
| `seatbelt report [runs] [--pubkey] [--json]` | usage by person, model and tool; refused, failed, open and unsigned runs. With no `runs`, this machine's runs | a ledger is broken, or forged with a key given |
| `seatbelt runs [--limit]` | lists this machine's runs by name, newest first | |
| `seatbelt verify [run or ledger] [--pubkey]` | checks the hash chain and attestation; a local run against this machine's key. With no argument, the latest run | broken, forged, incomplete, or unattested with a key given |
| `seatbelt reconstruct [run or ledger] [--pubkey]` | prints the run as a timeline; with no argument, the latest run | same as verify |
| `seatbelt demo [--out runs]` | records a scripted example run | |
| `seatbelt keygen [dir]` | writes an Ed25519 key pair | a key file exists |
| `seatbelt attest <ledger> --key` | signs a finished ledger into `<id>.attest.json` | broken or incomplete chain, sidecar exists |
| `seatbelt scenarios <corpus> --target m:f [--out] [--key] [--list] [--image] [--target-dir] [--timeout]` | runs the adversarial corpus | any finding (2: bad target) |
| `seatbelt pack <runs> --out <zip> [--key] [--corpus]` | bundles a runs directory into an evidence pack | broken ledger, output exists |
| `seatbelt verify-pack <zip> [--pubkey]` | re-checks a pack offline | forged, or a broken ledger inside |
| `seatbelt gateway keygen --user <id> [--config]` | issues a gateway key; stores only its hash | the user already has a key |
| `seatbelt gateway serve [--config]` | runs the recording gateway | bad config or signing key |

Every command has `--help`. Formats: ledger and attestation in [ADR 0001](docs/adr/0001-hash-chained-jsonl-ledger.md) and [ADR 0002](docs/adr/0002-signed-run-manifest.md), scenario and findings schemas in [`docs/schema/`](docs/schema/), evidence pack in [`docs/spec/evidence-pack-v1.md`](docs/spec/evidence-pack-v1.md).

## Status

Pre-1.0 and moving quickly: the ledger schema may change between minor versions. See [ROADMAP.md](ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md).

## Project

- [Issues](https://github.com/ronanpdh/seatbelt/issues) for bugs, questions and ideas
- [CONTRIBUTING.md](CONTRIBUTING.md) and [STANDARDS.md](STANDARDS.md)
- [SECURITY.md](SECURITY.md) for reporting vulnerabilities
- [Code of conduct](CODE_OF_CONDUCT.md)
- Licensed under [Apache-2.0](LICENSE)
