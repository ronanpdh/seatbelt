# Evidence pack design (0.1.0, part 2)

Date: 2026-09-17. Status: agreed, not yet implemented.

## Problem

A reviewer receives ledgers, attestation sidecars and a findings file as loose files. Nothing binds the set together, so a file can be dropped or swapped without detection, and nothing says which harness, corpus and key produced them. STANDARDS asks for an evidence pack format documented as a versioned spec.

## Decisions

- **One zip per pack**, `<name>.seatbelt.zip`, built with stdlib `zipfile`. Members: `runs/<id>.jsonl`, `runs/<id>.attest.json` where present, `findings.json` if present, `corpus/<id>.yaml` when a corpus is supplied, and `pack.json` last.
- **Signed manifest** `pack.json`: `pack_version`, `harness`, `created_at`, `members` (path, sha256, bytes for every file except `pack.json`), `runs` (run_id, events, ok, attested), `findings` count and `corpus_sha256` from `findings.json` (null when absent), `public_key`, `signature` (Ed25519 over the canonical form minus `signature`, same key and canonical rules as attestation). Signing is optional; an unsigned pack verifies as UNSIGNED.
- **Scope is a runs directory.** `seatbelt pack runs/ --out audit.seatbelt.zip [--key] [--corpus]`. Works for plain recorded runs and scenario runs alike.
- **Build refuses a broken ledger.** A pack must never launder a bad record. INCOMPLETE ledgers are packed and recorded as `ok: false`.
- **Deterministic.** Zip entries carry a fixed timestamp and members are written in sorted order, so two builds of the same inputs are byte-identical.
- **Verify re-checks everything offline.** Manifest version, signature, member set equals zip contents, each member hash, every ledger chain, every attestation, and that every finding's evidence ids exist in its ledger. First failure wins and names the member. Extraction goes to a temporary directory after rejecting `..`, absolute paths and duplicate entry names; `zipfile` never creates symlinks, so a symlink entry lands as a plain file.

Rejected: a plain directory (no integrity for the set until zipped), one inlined JSON document (duplicates ledgers, unbounded size), rendering to PDF or HTML (a later feature over the same pack).

## Module

`src/seatbelt/report/pack.py`:

- `PackError`, `PackManifest` (Pydantic, `extra="forbid"`, `canonical()`), `Member`, `RunSummary`.
- `build(runs_dir, out, *, signer=None, corpus=None) -> PackManifest`.
- `verify_pack(path, pubkey=None) -> PackVerdict(status, ledgers: list[LedgerStatus], reason)`, where `status` is `attested | unsigned | unchecked | forged` and each ledger is `ok | incomplete | broken` with its attestation status.

Schema exported to `docs/schema/pack.json` by `scripts/export_schemas.py`.

## CLI

- `seatbelt pack <runs_dir> --out <zip> [--key seatbelt.key] [--corpus scenarios/]`
- `seatbelt verify-pack <zip> [--pubkey seatbelt.pub]`: table of members with status, one summary line; exit 1 on FORGED or any broken ledger.

## Spec

`docs/spec/evidence-pack-v1.md`: zip layout, one table per manifest field (name, type, meaning, who sets it), the verification algorithm as numbered steps, what each verdict means, and the trust boundary inherited from ADR 0002.

## Tests

`tests/unit/test_pack.py`: build from a scenario run; determinism; clean verify; tamper matrix (edited ledger byte, dropped member, stray member, edited manifest field, swapped sidecar, wrong key) each FORGED naming the member; broken source ledger refused at build; incomplete packed as `ok: false`; dangling evidence id FORGED; unsigned and unchecked paths; corpus hash mismatch refused; zip-slip entry FORGED with nothing written outside the temp dir; CLI exit codes; schema currency.

## Docs

ADR 0004, `docs/spec/evidence-pack-v1.md`, CHANGELOG, ROADMAP item Done, README "Hand it over" block, walkthrough step 8.

## Out of scope

Docker sandbox, OpenSSF badge, rendered reports, pack-of-packs, timestamp authority.
