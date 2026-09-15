# Changelog

All notable changes to seatbelt are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/). Pre-1.0, minor versions may break the ledger schema; each such change gets a schema version bump and a note here.

## [Unreleased]

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

[Unreleased]: https://github.com/ronanpdh/seatbelt/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.2
[0.0.1]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.1
