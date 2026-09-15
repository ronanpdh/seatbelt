# Changelog

All notable changes to seatbelt are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/). Pre-1.0, minor versions may break the ledger schema; each such change gets a schema version bump and a note here.

## [Unreleased]

### Added
- `Recorder.tool_called` and `Recorder.tool_returned` primitives for adapters that observe tool calls without executing them; `tool_call` context manager now built on them.
- `gen_ai.tool.call.id` attribute on `tool.call` events (provider's own call id).
- `Adapter` protocol (`seatbelt.adapters.base`).
- Anthropic Messages API adapter (`seatbelt.adapters.anthropic`): records requests, responses with provider-returned model version and token usage, `tool_use` blocks as `tool.call`, and matching `tool_result` blocks as `tool.result`.
- Optional dependency group `anthropic`.
- Recorded fixture `tests/fixtures/anthropic_refund.json` and replay tests; adapter tests skip when the extra is absent.
- `examples/anthropic_refund.py`, a one-tool live example.

### Fixed
- Redaction now converts Pydantic models to JSON before redacting. SDK content blocks passed back in the request history were previously neither redacted nor hashable, so the Anthropic adapter's `create` raised.
- `seatbelt verify` reports a corrupt or missing ledger as BROKEN (exit 1) instead of raising.
- The `seatbelt` console script pointed at a missing `seatbelt:main`.
- Package version now comes from `pyproject.toml`.
- CI now actually runs on Python 3.13; previously every matrix leg used 3.12.

### Planned for this release
- OpenAI Agents SDK adapter (tracing processor).

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

[Unreleased]: https://github.com/ronanpdh/seatbelt/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.1
