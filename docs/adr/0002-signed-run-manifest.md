# 2. Signed run manifest

- Status: accepted
- Date: 2026-09-17

## Context

ADR 0001 detects any change inside the chain but not a rewritten tail: drop the last events, append a fresh `run.end` with a matching count, and `verify` passes. The final hash has to be recorded somewhere the ledger writer cannot rewrite.

## Decision

A signed manifest per run, `<run id>.attest.json` beside the ledger, holding the final event hash, the event count, the schema version, the run id and the SHA-256 of the ledger file, signed with Ed25519. Keys are PEM files written by `seatbelt keygen`; `seatbelt verify --pubkey` checks the signature with the reviewer's own copy of the public key, never the one embedded in the manifest.

Rejected: HMAC (whoever can verify can forge), Sigstore keyless (network at sign time, heavy dependency; the right next step for cross-organisation trust), an inline `run.attest` event (a schema bump, and a rewriter drops it like any tail).

## Consequences

- A truncated or rewritten tail, and any byte change to the file, is reported as FORGED when the public key is supplied. Without it the sidecar is UNCHECKED. Without a sidecar the ledger is UNATTESTED, which is exit 1 when a key was supplied (deleting the sidecar is the cheapest tamper) and a plain status otherwise. Neither downgrades the chain check.
- The key lives with the process that writes the ledger, so attestation proves the record has not changed since run end by anyone without the key. It does not prove the process was honest. The manifest identifies the run by `run_id`, not by file name, so a ledger and sidecar copied together under another name still verify.
- Signing happens in `Recorder.start`'s `finally`, after `run.end`, so a failed run is signed too. `Recorder.start` with a signer refuses a run whose sidecar already exists, so signing cannot fail on a stale file and mask the run's own error.
- A sidecar is never overwritten, and `seatbelt attest` refuses a broken or incomplete chain. Re-signing means deleting the sidecar first, which is visible.
- The manifest carries its own `attest_version`; a verifier rejects versions it does not know.
- `cryptography` becomes a hard dependency.
