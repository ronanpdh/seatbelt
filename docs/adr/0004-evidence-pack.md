# 4. Evidence pack: a zip bound by a signed manifest

- Status: accepted
- Date: 2026-09-17

## Context

Ledgers, attestation sidecars and a findings report reach a reviewer as loose files. Nothing binds the set: a file can be dropped or swapped, and nothing says which harness, corpus and key produced them. STANDARDS asks for a pack format documented as a versioned spec.

## Decision

One zip per pack with a signed `pack.json` manifest listing every member's SHA-256 and size, one summary row per run, the findings count and corpus hash, signed with the attestation key. `seatbelt pack` builds it from a runs directory; `seatbelt verify-pack` re-verifies the manifest, every member, every chain, every attestation and every finding's evidence offline. The format is `docs/spec/evidence-pack-v1.md`.

The builder refuses a broken ledger: a pack must never launder a bad record. Incomplete ledgers are packed and marked. Zip entries carry a fixed timestamp and sorted order so members are byte-identical across builds.

Rejected: a plain directory (nothing binds the set until it is zipped anyway), one inlined JSON document (duplicates every ledger and grows without bound), rendered PDF or HTML output (a later layer over the same pack).

## Consequences

- A reviewer gets one object, one command and one verdict, and can still open any ledger inside with `verify` and `reconstruct`.
- Verification re-runs the chain and attestation checks rather than trusting the manifest's summary rows, so the pack adds integrity for the set without weakening the per-ledger guarantees.
- The manifest's `pack_version` lets the format change without silent misreads; a change is a spec revision and a schema update.
- Extraction is to a temporary directory after path checks; a pack cannot write outside it.
- `created_at` comes from the builder's clock. A timestamp authority is out of scope.
