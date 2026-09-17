# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Seatbelt is a model-agnostic harness for AI agents. It records an agent run as a tamper-evident, hash-chained JSONL ledger that can be verified and replayed as a timeline. Pre-1.0: the ledger schema may change between minor versions.

## Commands

```sh
uv sync                                   # package + dev group (includes the optional framework SDKs)
uv run pytest                             # all tests
uv run pytest tests/unit/test_ledger.py::test_chain_links_and_verifies   # one test
uv run ruff check . && uv run ruff format --check .
uv run pyright                            # strict; covers src, tests, examples
uv run pre-commit run --all-files         # everything CI checks, plus whitespace/EOF fixers
uv run seatbelt demo --out runs           # write a sample ledger
uv run seatbelt verify runs/<id>.jsonl    # exit 1 if broken, incomplete, corrupt or missing
uv run seatbelt reconstruct runs/<id>.jsonl
scripts/walkthrough.sh [workdir]         # live Anthropic run + verify, reconstruct, lineage, tamper (costs cents)
```

CI (`.github/workflows/ci.yml`) runs `uv sync --locked`, ruff, pyright and pytest on Python 3.12 and 3.13. If you change dependencies, run `uv lock` or CI fails.

If a pre-commit hook modifies files, `git add -u` and commit again; committing with unstaged changes to the same files makes pre-commit roll back its fixes.

## Architecture

Data flows one way: **adapter → Recorder → redact → Ledger (hash chain) → file**, and back out through **verify** and **report**.

- `ledger/events.py`: `Event` and `Actor` are Pydantic models with `extra="forbid"`, so a key injected into a line fails to parse instead of vanishing from the canonical form. `canonical()` is sorted-key compact JSON of every field except `hash`; `hash` is its SHA-256 and `prev_hash` links to the previous event (first event uses `GENESIS_HASH`). Adding or renaming an `Event` field changes every hash, so it's a schema change: bump `SCHEMA_VERSION` in `events.py` and add a changelog note. Releases: bump with `uv version X.Y.Z`, move `[Unreleased]` to `[X.Y.Z]` in CHANGELOG.md, tag `vX.Y.Z`; `release.yml` refuses a tag that doesn't match the package version and uses the CHANGELOG section as release notes.
- `ledger/store.py`: `Ledger` appends one sealed event per line (file mode 0600, fsync per event) and, on construction, reads an existing file to resume `seq` and the last hash. `read_events` is the read path used by verify and report.
- `ledger/redact.py`: regex secret scrubbing, recursive over dicts, lists, tuples and Pydantic models (models are dumped to JSON first, which is also what makes SDK objects hashable).
- `record/recorder.py`: the only API adapters call. Every write goes through `Recorder._emit`, which always redacts before appending; nothing else should call `Ledger.append`. `Recorder.start` is a context manager that emits `run.start`/`run.end` (with `run.ok=False` and `run.error` on exception); it refuses an existing ledger file and a `run_id` that is not a safe filename. `model_call` records an error `model.response` if the body raises before `respond`. Lineage is via `parent_id` (response→request, tool result→tool call, policy check→subject, action→decision).
- `adapters/`: one module per framework, translation only, no interpretation. Framework SDKs are optional dependencies (extras, also in the dev group). The Anthropic adapter wraps `client.messages` (sync/async, `create`/`stream`, or `client.beta`) through one `_Recording._turn` context manager: `tool_use` blocks in a response become `tool.call` events, and matching `tool_result` blocks in the *next* request's history become `tool.result` events. The OpenAI Agents adapter is a `TracingProcessor`: it records agent spans as decisions in `on_span_start` (authority precedes the actions it covers) and everything else in `on_span_end`; default agents emit `ResponseSpanData`, not `GenerationSpanData`, so both are handled.
- `verify/chain.py`: `verify_events` checks seq order, `prev_hash` links and content hashes, returning a `Verdict` with the first bad seq. A separate `complete` flag requires a single `run.start` at seq 0 and a final `run.end` with a matching `run.events` count (CLI: INCOMPLETE, exit 1); a forged tail passes the chain check alone; attestation catches it (`docs/adr/0002-signed-run-manifest.md`).
- `attest/`: `manifest.py` builds a `Manifest` (run id, schema version, event count, final hash, SHA-256 of the file bytes; `extra="forbid"`, canonical form excludes only `signature`) and `sign.py` signs it with Ed25519 into `<run_id>.attest.json` beside the ledger. Keys are PEM files from `seatbelt keygen`; private key, public key and sidecar are created 0600 with `O_EXCL` and never overwritten. `Recorder.start(signer=...)` signs in its `finally` after `run.end` (failed runs included) and refuses a run whose sidecar exists. `verify/attest.py` trusts only the `--pubkey` file, never the key embedded in the manifest: no sidecar is UNATTESTED (exit 1 if a key was given), sidecar without key is UNCHECKED, mismatch or bad signature is FORGED (exit 1). `seatbelt attest` refuses a broken or incomplete chain.
- `report/timeline.py`: Rich table reconstruction; `_describe` has a case per `Kind`, so add one when adding a new `Kind`. Cells are `rich.text.Text`, never markup strings: ledger content is untrusted.
- `cli.py`: Typer app, exposed as the `seatbelt` console script; `reconstruct` verifies first and refuses a broken ledger. The package version comes from `pyproject.toml` via `importlib.metadata`.

Event `attrs` keys follow OpenTelemetry GenAI semantic conventions where one exists (`gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.tool.*`); anything else is namespaced (`policy.*`, `decision.*`, `action.*`, `outcome.*`).

## Conventions (from STANDARDS.md)

- Tests never call live models or the network. Adapter tests replay recorded responses from `tests/fixtures/`; they `importorskip` the SDK. `test_anthropic_transports.py` drives the real SDK over `httpx2.MockTransport` (JSON for `create`, synthesized SSE for `stream`), so SDK parsing is exercised too.
- Record the exact provider-returned model version (`response_model`), not just the requested model.
- Pyright strict: `Any` needs a comment explaining why; prefer `cast` with a reason over broad ignores.
- Keep a Changelog (add to `[Unreleased]`), ADRs in `docs/adr/`.

## Working rules

- Comments only where vital (a non-obvious why). No verbose or text-heavy comments or docstrings.
- Commits and PR titles use Conventional Commits: `feat:`, `fix:`, `chore:` (plus `docs:`, `ci:`, `test:` where apt).
- PR descriptions are short: a few lines at most, no walls of text.
- Always stop before pushing. Run the pre-commit hooks and a code review, report the results, and wait for the user's go-ahead.
- GitHub Actions: check a version tag exists before referencing it (`astral-sh/setup-uv` has no floating major tags, so it is pinned by SHA).
- STANDARDS.md and ROADMAP.md describe planned modules (`scenarios/`, Docker sandboxing, evidence packs) that do not exist yet.
