<img src="https://raw.githubusercontent.com/ronanpdh/seatbelt/main/docs/assets/seatbelt-cover.png" alt="Seatbelt" width="100%">

# seatbelt

[![ci](https://github.com/ronanpdh/seatbelt/actions/workflows/ci.yml/badge.svg)](https://github.com/ronanpdh/seatbelt/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/ronanpdh/seatbelt/badge)](https://scorecard.dev/viewer/?uri=github.com/ronanpdh/seatbelt)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/15081/badge)](https://www.bestpractices.dev/projects/15081)

Seatbelt records what your AI agents do: every prompt, model response and tool call. The record is tamper-evident and signed, so you can prove later exactly what happened.

## Quick start

```sh
uv tool install seatbelt-ai   # needs uv: https://docs.astral.sh/uv/
seatbelt run claude           # or: seatbelt run codex, seatbelt run gemini
```

The package is `seatbelt-ai`; the command is `seatbelt`. `pip install seatbelt` and `uvx seatbelt` fetch an unrelated project, so run once without installing with `uvx --from seatbelt-ai seatbelt run claude`. Only one `seatbelt` command can be installed at a time: if you installed from git before 0.5.0, run `uv tool uninstall seatbelt` first.

Use the CLI as you normally would; it keeps its own sign-in, whether that's a subscription or an API key. When you exit, seatbelt prints the run's name (e.g. `claude-99ce72ff`) and saves a signed record of it. Then:

```sh
seatbelt reconstruct                 # replay your latest run as a timeline
seatbelt reconstruct claude-99ce72ff # or a run by name
seatbelt runs                        # list your runs by name
seatbelt report                      # what your runs did: models, tools, tokens, anything refused or altered
```

There is nothing to set up. How it works, where runs are saved, and what is and isn't recorded: [docs/local-recording.md](https://github.com/ronanpdh/seatbelt/blob/main/docs/local-recording.md).

No agent to hand? `seatbelt demo` records an example run in `./runs` and prints the commands that check and replay it (`seatbelt verify runs/<run id>.jsonl`). `verify` and `reconstruct` take a file path, or the name of a run `seatbelt run` recorded, from `seatbelt runs`.

## What you get

- **One file per run** (JSONL): user messages, model requests and responses (with the exact model version and token usage), tool calls and results, and policy checks.
- **Tamper evidence.** Each event is SHA-256 hash-chained to the one before it. An edit, reorder or deletion breaks the chain, and `seatbelt verify` fails and names the first bad event.
- **A signature.** A signed manifest pins the final hash and the file's SHA-256. Anyone who can write the file can also recompute the chain, so a ledger that was re-chained, or cut short and closed again, is caught for certain only with `--pubkey` against a signed sidecar. `verify` checks a local run against this machine's key; for any other ledger, without `--pubkey` it does not check the signature and says `unchecked` or `unattested` (or FORGED, when the signature file does not match the ledger).
- **Redaction.** Known secret formats are removed before anything is hashed or written: Anthropic, OpenAI, Google and Stripe API keys, seatbelt's own `sbk_` keys, AWS access key ids, GitHub and Slack tokens, bearer tokens, JWTs, private key blocks, passwords in URLs, and `…KEY=`, `…TOKEN=`, `…SECRET=` and `…PASSWORD=` lines as in `.env` files (an upper-case name and a value of 16 characters or more). A secret in another shape is recorded as it was sent.

## Record for a team

To record everyone's agents in one place, run a gateway ([docs/deploy/gateway.md](https://github.com/ronanpdh/seatbelt/blob/main/docs/deploy/gateway.md)). Each person then adds two lines to `~/.config/seatbelt/config.toml`, and `seatbelt run` records through the gateway instead:

```toml
gateway = "https://gw.corp.example"
key = "sbk_..."
```

The gateway holds the provider keys and signs one ledger per session. It can also:
- restrict models, output tokens and tools;
- accept Claude Desktop users signed in with your identity provider;
- ship every ledger to object storage.

On Claude Enterprise, `seatbelt import compliance` also brings in what no gateway sees: claude.ai chats, and Cowork and Claude Code sessions, from Anthropic's Compliance API ([docs/deploy/compliance-import.md](https://github.com/ronanpdh/seatbelt/blob/main/docs/deploy/compliance-import.md)).

`seatbelt report runs --pubkey keys/seatbelt.pub` summarises who used what.

## Record your own agent

Install the library into your agent's environment, with the extra for your SDK:

```sh
pip install "seatbelt-ai[anthropic]"      # or: uv add "seatbelt-ai[openai-agents]"
```

The import name is `seatbelt`, which an unrelated PyPI project also uses: install `seatbelt-ai`, never `seatbelt`, and don't install both in one environment. The `uv tool install` in the quick start puts the CLI in its own environment, where your code can't import it.

```python
from pathlib import Path
from seatbelt.record.recorder import Recorder

with Recorder.start(Path("runs"), agent_id="support-bot") as rec:
    rec.user_message("u-42", "Refund order 1001")
    with rec.tool_call("lookup_order", {"order": 1001}) as tool:
        tool.result({"status": "delivered"})
    rec.outcome("refund issued", success=True)
```

Using the Anthropic SDK? Wrap the client and every `create` or `stream` call is recorded, tool calls included: `AnthropicAdapter(rec).messages(client)`, `.messages(client.beta)`, or `.async_messages(async_client)`. See [`examples/anthropic_refund.py`](https://github.com/ronanpdh/seatbelt/blob/main/examples/anthropic_refund.py).

Using the OpenAI Agents SDK? Register `agents.add_trace_processor(SeatbeltProcessor(rec))` once at startup. See [`examples/openai_agents_refund.py`](https://github.com/ronanpdh/seatbelt/blob/main/examples/openai_agents_refund.py).

To sign each run when it ends, make a key with `seatbelt keygen keys` and pass `signer=Signer.from_file(Path("keys/seatbelt.key"))` to `Recorder.start` (`from seatbelt.attest.sign import Signer`).

## Test your agent

The shipped corpus feeds prompt injection, tool-argument smuggling, credential exfiltration and more to your agent, then checks the ledger. It can also run your agent in a sandbox with no network, and bundle the results into one signed evidence pack. See [docs/scenarios.md](https://github.com/ronanpdh/seatbelt/blob/main/docs/scenarios.md).

```sh
git clone https://github.com/ronanpdh/seatbelt && cd seatbelt && uv sync
uv run seatbelt scenarios scenarios/ --target examples.scenario_target:target   # one deliberate FAIL
```

## Commands

| Command | Does | Exit 1 when |
|---|---|---|
| `seatbelt run <cli> [--config] [--exe] [-- args]` | runs `claude`, `codex` or `gemini`, recorded on this machine or through your gateway | bad config, signing key or spawn error, or unknown CLI (127: executable not found); otherwise the CLI's own exit code, 128 + N when a signal N ended it |
| `seatbelt report [runs...] [--pubkey] [--people] [--json]` | usage by person, model and tool; refused, failed, open and unsigned runs. Takes several folders; `--people` joins one person's ids ([guide](https://github.com/ronanpdh/seatbelt/blob/main/docs/deploy/gateway.md#one-row-per-person)). With no `runs`, this machine's runs | a ledger is broken or forged, a signature's ledger is missing, or with a key given (with no `runs`, this machine's key) one ended unsigned; a bad people file |
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
| `seatbelt erase <runs...> (--principal <id> \| --person <name> --people <file>) --case <ref> [--key \| --config] [--yes]` | removes a person's ledgers inside a signed record; lists only without `--yes` ([guide](https://github.com/ronanpdh/seatbelt/blob/main/docs/deploy/erasure.md)) | a folder in use, a bad people file or config, no signing key; with `--yes`, an unreadable ledger that is or may be the person's |
| `seatbelt gateway keygen --user <id> [--config]` | issues a gateway key; stores only its hash | the user already has a key |
| `seatbelt gateway serve [--config]` | runs the recording gateway | bad config or signing key |
| `seatbelt import compliance [--config]` | imports Claude Enterprise transcripts (chats, Cowork, Claude Code and other app sessions) from Anthropic's Compliance API into signed ledgers; see [docs/deploy/compliance-import.md](https://github.com/ronanpdh/seatbelt/blob/main/docs/deploy/compliance-import.md) | bad config, key or API error, or a conversation that needs a person to check |

Every command has `--help`. Formats: ledger and attestation in [ADR 0001](https://github.com/ronanpdh/seatbelt/blob/main/docs/adr/0001-hash-chained-jsonl-ledger.md) and [ADR 0002](https://github.com/ronanpdh/seatbelt/blob/main/docs/adr/0002-signed-run-manifest.md), scenario and findings schemas in [`docs/schema/`](https://github.com/ronanpdh/seatbelt/tree/main/docs/schema/), evidence pack in [`docs/spec/evidence-pack-v1.md`](https://github.com/ronanpdh/seatbelt/blob/main/docs/spec/evidence-pack-v1.md).

## Status

Pre-1.0 and moving quickly: the ledger schema may change between minor versions. See [ROADMAP.md](https://github.com/ronanpdh/seatbelt/blob/main/ROADMAP.md) and [CHANGELOG.md](https://github.com/ronanpdh/seatbelt/blob/main/CHANGELOG.md).

## Project

- [Issues](https://github.com/ronanpdh/seatbelt/issues) for bugs, questions and ideas
- [CONTRIBUTING.md](https://github.com/ronanpdh/seatbelt/blob/main/CONTRIBUTING.md) and [STANDARDS.md](https://github.com/ronanpdh/seatbelt/blob/main/STANDARDS.md)
- [SECURITY.md](https://github.com/ronanpdh/seatbelt/blob/main/SECURITY.md) for reporting vulnerabilities
- [Code of conduct](https://github.com/ronanpdh/seatbelt/blob/main/CODE_OF_CONDUCT.md)
- Licensed under [Apache-2.0](https://github.com/ronanpdh/seatbelt/blob/main/LICENSE)
