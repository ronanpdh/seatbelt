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

## 0.1.0

- Adversarial scenario pack (`seatbelt.scenarios`) with a shipped YAML corpus, each scenario mapped to the OWASP Agentic Top 10 and, where one exists, a MITRE ATLAS technique
- Sandboxed target runs in Docker, no network unless a scenario declares egress
- Evidence pack format, documented as a versioned spec
- Signed releases (Sigstore), CycloneDX SBOM and SLSA provenance
- OpenSSF Best Practices badge
