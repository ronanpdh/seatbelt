# Evidence pack format, version 1

An evidence pack is one zip file that carries the ledgers of one or more agent runs, their attestation sidecars, the findings of a scenario run where there was one, and a manifest that binds them together. It is built by `seatbelt pack` and checked by `seatbelt verify-pack`. Nothing in it needs a network or a service to verify.

## Layout

| Entry | Required | Content |
|---|---|---|
| `pack.json` | yes | The manifest, always the last entry. |
| `runs/<run id>.jsonl` | one or more | A ledger exactly as written by the recorder. |
| `runs/<run id>.attest.json` | where present | The ledger's attestation sidecar (ADR 0002). |
| `findings.json` | when present in the source directory | The scenario report (`docs/schema/findings.json`). |
| `corpus/<scenario id>.yaml` | when `--corpus` was given | The scenario files whose hash `findings.json` names. |

Any other entry path is rejected. Every entry has the zip timestamp 1980-01-01 00:00:00 and entries are written in sorted order, so two packs of the same inputs differ only in `pack.json` (`created_at` and, if signed, `signature`).

## Manifest fields

Schema: `docs/schema/pack.json`. Unknown fields are rejected.

| Field | Type | Meaning | Set by |
|---|---|---|---|
| `pack_version` | integer | Format version of this document; this spec is 1. A verifier rejects versions it does not know. | builder |
| `harness` | string | `seatbelt <version>` that built the pack. | builder |
| `created_at` | RFC 3339 timestamp | When the pack was built, by the builder's clock. Informational. | builder |
| `members` | list of `{path, sha256, bytes}` | Every entry in the zip except `pack.json`, its SHA-256 hex digest and its size in bytes. | builder |
| `runs` | list of `{run_id, events, complete, attested}` | One row per ledger: its event count, whether the chain ends in a matching `run.end`, and whether a sidecar is present. | builder |
| `findings` | integer or null | Number of findings in `findings.json`; null when there is no report. | builder |
| `corpus_sha256` | string or null | Corpus hash copied from `findings.json`; null when there is no report. | builder |
| `public_key` | base64 string | Raw Ed25519 public key of the signer. Informational: a verifier trusts only the key file it is given. Empty when unsigned. | builder |
| `signature` | base64 string | Ed25519 signature over the canonical form. Empty when unsigned. | builder |

Canonical form: JSON of every field except `signature`, keys sorted, no whitespace, UTF-8.

## Verification

`seatbelt verify-pack <zip> [--pubkey <file>]` performs these steps in order and stops at the first failure, which it reports as FORGED with the offending member named:

1. If a public key file was given, load it. A file that is not an Ed25519 public key is an error, even before the pack is opened.
2. Open the zip. Reject any entry whose path is absolute, contains `..`, contains a backslash, or appears more than once.
3. Read `pack.json`. Reject a missing or unparseable manifest, or one whose `pack_version` is unknown.
4. Signature: empty means UNSIGNED; present but no key given means UNCHECKED; otherwise verify with the given key, FORGED on mismatch.
5. The list of entries other than `pack.json` (no name may repeat) must equal the `members` paths: an extra or missing entry is FORGED.
6. Each member's size and SHA-256 must match the manifest. The size is checked from the zip's central directory before the member is read; a member that fails to decompress is FORGED.
7. Extract to a temporary directory. For every row in `runs`: the ledger must exist, its chain is verified (ADR 0001), its attestation is verified against the given key (ADR 0002) and a forged attestation is FORGED. For an intact chain the event count, completeness and presence of a sidecar must match the row. A ledger entry not listed in `runs` is FORGED.
8. If `findings.json` is present it must parse, its finding count and corpus hash must match the manifest, and every finding's evidence ids must exist in that scenario's ledger. If the manifest counts findings but the file is absent, FORGED.
9. If `corpus/` is present the manifest must name a `corpus_sha256` and the directory must hash to it.

## Verdicts

| Verdict | Meaning | Exit code |
|---|---|---|
| `attested` | Signature verified with the given key and every step passed. | 0 |
| `unsigned` | No signature in the manifest; members, chains and attestations still verified. | 0, with a warning |
| `unchecked` | Signed, but no public key was given; everything except the pack signature verified. | 0, with a warning |
| `forged` | A step failed. The reason names the member. | 1 |

Independently of the pack verdict, each ledger is reported as `ok`, `incomplete` (chain intact, no matching `run.end`) or `broken`. A broken ledger makes the command exit 1 even in an attested pack: the builder refuses to pack one, so its presence means the pack was produced by something else.

## Trust boundary

The pack key is the attestation key (ADR 0002). A verified pack proves the set has not changed since it was built by whoever held the key. It does not prove the runs were honest, and `created_at` is the builder's own clock.
