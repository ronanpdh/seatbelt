# Changelog

All notable changes to seatbelt are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/). Pre-1.0, minor versions may break the ledger schema; each such change gets a schema version bump and a note here.

## [Unreleased]

### Added
- `Recorder.model_requested`, the request half of `model_call`, for callers that receive the response later.
- Scenario check `no_tool_success: <tool>`: fails on a successful `tool.result` for that tool, so a self-refusal, a policy denial and no call at all pass alike. `trust-exploitation-policy` uses it instead of `policy_denied`, which failed an agent that refused the refund without ever calling the tool.
- `examples/anthropic_scenario_target.py`: Claude as the support agent for the scenario corpus, recorded through the adapter, with a `refund-limit` policy check in the tool executor.
- `seatbelt.gateway.formats.anthropic`: the Anthropic Messages wire format on plain JSON (request, response, SSE reassembly), shared by the SDK adapter and the gateway.
- `seatbelt.gateway.formats.openai_chat`: the OpenAI Chat Completions wire format on plain JSON, including SSE reassembly, for the gateway.

### Changed
- The Anthropic adapter records every integer `usage` field the provider returns (cache tokens included), not only input and output tokens.

## [0.1.0] - 2026-09-18

### Added
- `docs/overview.md`: what the harness records, why the record proves itself, and how to use every command.
- OpenSSF Best Practices evidence (`docs/openssf-best-practices.md`), a bug-report and test policy in CONTRIBUTING.md, secure-design and cryptography statements in SECURITY.md, a command reference and CI/Scorecard badges in the README.
- Scenario pack (`seatbelt.scenarios`): a shipped corpus under `scenarios/` of adversarial single-turn scenarios, each mapped to the OWASP Top 10 for Agentic Applications (and MITRE ATLAS where one exists), run against a `target(rec, inputs)` callable with `seatbelt scenarios <corpus> --target module:func`. Checks (`no_tool_call`, `tool_call`, `policy_denied`, `no_match`, `run_ok`) are evaluated over the ledger and every finding cites the event ids that prove it. `--list` shows the corpus; `--key` signs each ledger. Schemas in `docs/schema/`. See ADR 0003.
- `examples/scenario_target.py`, a scripted agent with one deliberate flaw so the demo shows a finding.
- `pyyaml` is a dependency.
- Evidence pack (`seatbelt.report.pack`): `seatbelt pack <runs_dir> --out audit.seatbelt.zip [--key] [--corpus]` bundles ledgers, attestation sidecars, `findings.json` and the corpus into one zip with a signed `pack.json` manifest; `seatbelt verify-pack <zip> [--pubkey]` re-checks the signature, every member hash, every chain, every attestation and every finding's evidence offline. Format: `docs/spec/evidence-pack-v1.md`, ADR 0004, schema `docs/schema/pack.json`.
- `seatbelt.attest.manifest.Signed`, the shared base for signed documents; `Signer.sign` accepts any `Signed`.
- Docker sandbox for scenario targets: `seatbelt scenarios ... --image <img> [--target-dir .] [--timeout 120]` runs each scenario in its own hardened container (no network unless the scenario sets `egress: true`, read-only root, no capabilities, host uid) built on `docker/Dockerfile`, and records `sandbox.image`, `sandbox.image_digest` and `sandbox.egress` in `run.start`. The signing key stays on the host. `--list` shows egress. See ADR 0005.
- `Scenario.egress` (default false); `seatbelt.scenarios.runner.record` and `collect` for callers that produce ledgers another way.

### Fixed
- CLI messages are no longer word-wrapped at the terminal width, so a long path or reason stays on one line; the attest test failed in CI on a wrapped line.
- Pre-commit runs ruff on Markdown code blocks as CI does.

## [0.0.4] - 2026-09-17

### Added
- Attestation (`seatbelt.attest`): `Recorder.start(..., signer=Signer.from_file(key))` signs an Ed25519 manifest (final hash, event count, file digest) into `<run id>.attest.json` at run end, failed runs included. `seatbelt keygen` writes the key pair, `seatbelt attest <ledger> --key` signs after the fact, and `seatbelt verify --pubkey` reports attested, FORGED (exit 1), UNCHECKED (sidecar but no key) or unattested. A truncated or rewritten tail, which the chain check alone accepts, is now detected. See ADR 0002.
- `cryptography` is a dependency.

## [0.0.3] - 2026-09-17

### Security
- `Event` and `Actor` forbid unknown keys. A key injected into a ledger line previously survived `verify`, because the canonical form is a re-dump of the parsed model.
- Redaction patterns are anchored to token shapes: `bearer` needs a 16+ character token and `sk-` may not follow a letter, so ordinary prose (`the bearer of`, `desk-...`) is no longer destroyed in the record. Fine-grained GitHub tokens (`github_pat_`) are redacted.
- Ledger files are created mode 0600 and each event is fsynced.
- `Recorder.start` rejects a `run_id` that is not a safe filename and refuses to append to an existing ledger.

### Added
- Policy engine at the tool boundary (`seatbelt.policy.engine`): `Recorder.start(..., policy=Policy(*rules))` evaluates every `Rule` against each `Recorder.tool_call`, records one `policy.check` per rule linked to the `tool.call`, and raises `PolicyDenied` before the tool body runs if any rule denies. A rule that raises counts as a denial (fail closed) and is recorded as one. Built-in rules `allowlist(*tools)` and `denylist(*tools)`. The Anthropic and OpenAI Agents adapters only observe tool calls the framework dispatches, so they record no checks; enforce through `Recorder.tool_call`.
- `run.end` carries `run.error` (exception type and message) when a run fails, and a `model_call` whose body raises records a `model.response` with `error` before re-raising; likewise a `tool_call` whose body raises records a `tool.result` with `error`.
- `seatbelt reconstruct` verifies first: BROKEN (exit 1) on a tampered ledger, a warning on an incomplete one. `run.start` and `run.end` rows now show the harness version and `ok`/`FAILED: <error>`.
- `verify` reports `complete` only when the ledger has exactly one `run.start`, at seq 0.
- `seatbelt.ledger.store.read_events`.
- Anthropic adapter covers every call style: `messages.stream()`, `AsyncAnthropic` via `AnthropicAdapter.async_messages()`, and the beta endpoint by passing `client.beta`. All record the same ledger shape. A stream the caller abandons (break or `close()`) is recorded as what was received, with `stop_reason: null` and no tool calls; the wrapper never reads further, so exits behave exactly like the SDK's.
- Model requests also record `thinking`, `output_config`, `stop_sequences` and `betas`.

### Removed
- `seatbelt.adapters.base.Adapter` protocol and `AnthropicAdapter.attach`: nothing consumed the protocol, and `attach` did not affect wrappers already created.
- `Ledger.read`: use `seatbelt.ledger.store.read_events(path)`.

### Fixed
- `Ledger.append` is thread-safe. Concurrent appends from several threads previously corrupted the chain (sequence gaps).
- `seatbelt reconstruct` raised a traceback on a corrupt or missing ledger.
- `create(stream=True)` raised after recording the request, leaving it unanswered; it now fails up front with a pointer to `.stream()`.

## [0.0.2] - 2026-09-15

### Changed
- **Ledger schema version 1.** Every event now carries a required `schema_version`, and `verify` rejects versions it does not know. Ledgers written by 0.0.1 have no version and are rejected as an old format; they cannot be verified by 0.0.2.

### Added
- Release workflow: tagged builds attach the wheel, sdist and a CycloneDX SBOM to a GitHub release, with Sigstore-signed SLSA build provenance and SBOM attestations.
- `Recorder.tool_called` and `Recorder.tool_returned` primitives for adapters that observe tool calls without executing them; `tool_call` context manager now built on them.
- `gen_ai.tool.call.id` attribute on `tool.call` events (provider's own call id).
- `Adapter` protocol (`seatbelt.adapters.base`).
- Anthropic Messages API adapter (`seatbelt.adapters.anthropic`): records requests, responses with provider-returned model version and token usage, `tool_use` blocks as `tool.call`, and matching `tool_result` blocks as `tool.result`.
- Optional dependency group `anthropic`.
- Recorded fixture `tests/fixtures/anthropic_refund.json` and replay tests; adapter tests skip when the extra is absent.
- `examples/anthropic_refund.py`, a one-tool live example.
- OpenAI Agents SDK adapter (`seatbelt.adapters.openai_agents.SeatbeltProcessor`), a tracing processor: generation and response spans become `model.request`/`model.response`, function spans `tool.call`/`tool.result` (MCP data under `mcp.*`), agent, handoff and guardrail spans `decision` events with the agent as authority.
- Optional dependency group `openai-agents`; `examples/openai_agents_refund.py`, the same refund agent.
- `Recorder.tool_called` accepts extra `attrs` and a `parent_id`; both adapters link each `tool.call` to the `model.response` that requested it.
- `seatbelt verify` reports INCOMPLETE (exit 1) when a chain is intact but does not end in a `run.end` whose `run.events` count matches, catching a truncated tail.
- `examples/anthropic_walkthrough.py`, a live run exercising every event kind.

### Fixed
- `seatbelt reconstruct` rendered ledger text as Rich markup, so bracketed text such as `[authority: ...]` vanished and ledger content could restyle or hide cells. Cells are now literal, and summaries are one line, at most 80 characters.
- Redaction now converts Pydantic models to JSON before redacting. SDK content blocks passed back in the request history were previously neither redacted nor hashable, so the Anthropic adapter's `create` raised.
- `seatbelt verify` reports a corrupt or missing ledger as BROKEN (exit 1) instead of raising.
- The `seatbelt` console script pointed at a missing `seatbelt:main`.
- Package version now comes from `pyproject.toml`.
- CI now actually runs on Python 3.13; previously every matrix leg used 3.12.

## [0.0.1] - 2026-09-14

### Added
- Event model (`Event`, `Actor`, `Kind`) with canonical JSON hashing and a genesis hash.
- Append-only JSONL `Ledger` with a SHA-256 hash chain per run; resumes after restart.
- Redaction of common secret formats before hashing and writing.
- `Recorder` API: run start and end (with failure flag), user message, model call, tool call, policy check, decision (with authority and basis), action, outcome.
- `verify_events` and `verify_file` with a `Verdict` naming the first bad sequence number.
- Timeline reconstruction (`report.timeline`).
- CLI: `seatbelt version`, `seatbelt verify <ledger>` (exit 1 on tamper), `seatbelt reconstruct <ledger>`, `seatbelt demo`.
- Tests: hash determinism, chain link and resume, property test that any edit breaks the chain, deletion detection, redaction, recorder lineage and failure path, CLI round trip and tamper detection.
- Project scaffolding: uv, ruff, pyright strict, pytest, Hypothesis, pre-commit, CI, Dependabot, Scorecard, SECURITY.md, CONTRIBUTING.md, STANDARDS.md, ROADMAP.md.

[Unreleased]: https://github.com/ronanpdh/seatbelt/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.1.0
[0.0.4]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.4
[0.0.3]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.3
[0.0.2]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.2
[0.0.1]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.1
