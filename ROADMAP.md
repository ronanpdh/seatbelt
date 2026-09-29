# Roadmap

Versions are targets, not promises. Released work is recorded in [CHANGELOG.md](CHANGELOG.md).

## 0.0.1 (released)

Event model, hash-chained JSONL ledger, redaction, Recorder API, verification, timeline reconstruction, CLI.

## 0.0.2 (released)

Anthropic Messages API and OpenAI Agents SDK adapters, ledger schema version, signed releases with SBOM and provenance.

## 0.0.3 (released)

Policy engine at the tool boundary, Anthropic adapter for stream/async/beta calls, ledger hardening (unknown keys rejected, 0600 + fsync, thread-safe appends).

## 0.0.4 (released)

Signed Ed25519 attestation of each run (`seatbelt.attest`, `keygen`, `attest`, `verify --pubkey`), so a truncated or rewritten ledger tail is detectable.

## 0.1.0 (released)

- Done: adversarial scenario pack (`seatbelt.scenarios`) with a shipped YAML corpus, each scenario mapped to the OWASP Agentic Top 10 and, where one exists, a MITRE ATLAS technique
- Done: sandboxed target runs in Docker (`seatbelt scenarios --image`), no network unless a scenario declares egress
- Done: evidence pack format (`seatbelt pack`, `seatbelt verify-pack`), documented as a versioned spec in `docs/spec/evidence-pack-v1.md`
- Done: signed releases (Sigstore), CycloneDX SBOM and SLSA provenance (since 0.0.2)
- Done (repository side): OpenSSF Best Practices badge, evidence in `docs/openssf-best-practices.md`; registered as bestpractices.dev project 15081, badge in the README

## 0.2.0 (released)

- Done: recording gateway (`seatbelt gateway serve`) for the Anthropic Messages API and OpenAI Chat Completions, streamed or not, with the Claude Code and Claude Desktop probe endpoints
- Done: per-employee issued keys (`seatbelt gateway keygen`), stored as hashes; principal and key id in every ledger
- Done: one signed ledger per employee session, idle close, named runs, crash-safe close on restart
- Done: org policy at the gateway: allowed models, output token cap, denied tools
- Done: launcher, `seatbelt run claude`
- Done: fleet report, `seatbelt report`
- Done: gateway Docker image (`docker/Dockerfile.gateway`) and deployment docs, including Claude Desktop and Cowork via MDM (`docs/deploy/`)

## 0.3.0 (released)

- Done: OpenAI Responses format (current Codex, the OpenAI Agents SDK) and a `codex` launcher preset
- Done: local recording, `seatbelt run` with no gateway: the CLI keeps its own sign-in (subscriptions included) and each run is signed on the machine
- Done: Gemini `generateContent` format (Gemini CLI, Google Gen AI SDKs) and a `gemini` launcher preset
- Done: config reload when the file changes or on SIGHUP, so a new or revoked key takes effect without a restart
- Done: central sink, shipping each signed ledger to S3-compatible object storage (Hetzner Object Storage) at run end
- Done: OIDC sign-in for Claude Desktop (`inferenceGatewayOidc`), users identified by the provider's immutable id
- Done: Compliance API importer (`seatbelt import compliance`) for Claude Enterprise transcripts (claude.ai chats, Cowork, and Claude Code and other app sessions) where clients are not routed through the gateway; tested against a fake of the documented API, not a live tenant; guide in `docs/deploy/compliance-import.md`
