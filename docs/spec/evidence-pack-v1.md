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
| `runs` | list of `{run_id, events, complete, attested}` | One row per ledger: its event count, whether the chain ends in a matching `run.end`, and whether a sidecar is present. `run_id` is the ledger's file name without `.jsonl`; it matches `^[A-Za-z0-9_.-]*[A-Za-z0-9_-][A-Za-z0-9_.-]*$` (the recorder's run-id rule: no `/`, not only dots) and appears once. | builder |
| `findings` | integer or null | Number of findings in `findings.json`; null when there is no report. | builder |
| `corpus_sha256` | string or null | Corpus hash copied from `findings.json`; null when there is no report. | builder |
| `public_key` | base64 string | Raw Ed25519 public key of the signer. Informational: a verifier trusts only the key file it is given. Empty when unsigned. | builder |
| `signature` | base64 string | Ed25519 signature over the canonical form. Empty when unsigned. | builder |

Canonical form: JSON of every field except `signature`, keys sorted, no whitespace, UTF-8.

The builder refuses a ledger whose chain is broken, whose file name is not a valid run id, or whose sidecar does not match it. With a signing key it also refuses a sidecar that does not verify with that key, so the pack signature never covers a sidecar the signer did not check.

## Verification

`seatbelt verify-pack <zip> [--pubkey <file>]` performs these steps in order and stops at the first failure, which it reports as FORGED with the offending member named:

1. If a public key file was given, load it. A file that is not an Ed25519 public key is an error, even before the pack is opened.
2. Open the zip. Reject any entry whose path is absolute, contains `..`, contains a backslash, or appears more than once, and any entry that is encrypted or uses a compression method other than stored or deflated.
3. Read `pack.json`. Reject one larger than 16 MiB (checked before it is inflated), a missing, corrupt or unparseable manifest, or one whose `pack_version` is unknown.
4. Signature: empty means UNSIGNED; present but no key given means UNCHECKED; otherwise verify with the given key, FORGED on mismatch. When a key was given, an UNSIGNED pack fails the check (exit 1, see Verdicts), as a missing sidecar does for `seatbelt verify --pubkey` (ADR 0002).
5. The list of entries other than `pack.json` (no name may repeat) must equal the `members` paths: an extra or missing entry is FORGED.
6. Bounds: the members' declared sizes may not add up to more than 8 GiB, no member may be larger than 2 GiB, and a member larger than 16 MiB may not be compressed more than 200:1. Each member's size in the zip's central directory must match the manifest before it is read. Each member is then inflated once, in bounded chunks and never past its declared size, into a temporary directory, and its size and SHA-256 must match the manifest; a member that fails to decompress is FORGED.
7. For every row in `runs`: `runs/<run id>.jsonl` must be a member, its chain is verified (ADR 0001), and its attestation is verified against the given key (ADR 0002). A forged attestation is FORGED, and so is a sidecar whose run id, event count, final hash or file hash contradicts the ledger even when no key was given. When a key was given, a ledger without a sidecar fails the check (exit 1). For an intact chain the event count, completeness and presence of a sidecar must match the row. A ledger or sidecar entry that belongs to no row in `runs`, and a run listed twice, are FORGED.
8. If `findings.json` is present it must parse, its finding count and corpus hash must match the manifest, every finding's scenario id must be a valid run id, and every finding's evidence ids must exist in that scenario's ledger. If the manifest counts findings but the file is absent, FORGED.
9. If `corpus/` is present the manifest must name a `corpus_sha256` and the directory must hash to it.

## Verdicts

| Verdict | Meaning | Exit code |
|---|---|---|
| `attested` | Signature verified with the given key and every step passed. | 0, unless a ledger has no sidecar (`UNATTESTED`): then 1 |
| `unsigned` | No signature in the manifest; members, chains and attestations still verified. | 0 with a warning when no key was given; 1 when a key was given |
| `unchecked` | Signed, but no public key was given; everything except the pack signature verified. | 0, with a warning |
| `forged` | A step failed. The reason names the member. | 1 |

Independently of the pack verdict, each ledger is reported as `ok`, `incomplete` (chain intact, no matching `run.end`) or `broken`. A broken ledger makes the command exit 1 even in an attested pack: the builder refuses to pack one, so its presence means the pack was produced by something else.

A key is what makes the check worth relying on. Without one, an attacker who can rewrite the pack can re-chain a ledger, drop or rewrite its sidecar and repack it unsigned; the result is `unsigned` or `unchecked` and exits 0. With a key, the only passing result is `attested` with every ledger's sidecar present and verified. Library callers get the same rule from `PackVerdict.ok`.

## Trust boundary

The pack key is the attestation key (ADR 0002). A verified pack proves the set has not changed since it was built by whoever held the key. It does not prove the runs were honest, and `created_at` is the builder's own clock.
