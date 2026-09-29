# Removing one person's ledgers on request (0.4.0): design

**Goal:** `seatbelt erase` removes every ledger recorded under a given person's principal ids from the folders it is given. It leaves a signed record that the removal happened, and it never edits a ledger.

**What it is not.** It does not decide whether a request must be honoured. The GDPR gives a right to erasure "without undue delay" [G1, Art. 17(1)], and also names cases where that right does not apply. Among them is processing that is necessary "for compliance with a legal obligation", or "for the establishment, exercise or defence of legal claims" [G1, Art. 17(3)(b) and (e)]. Which applies to a given request is for the organisation to decide (ours). seatbelt provides the mechanism and records the organisation's case reference.

**Status:** implemented as `seatbelt erase` (`seatbelt.erase`, `seatbelt.locks`); the guide is `docs/deploy/erasure.md`. The design was checked claim by claim against its sources by a second reader, and the maintainer's decisions are under "Decided".
- Legal statements quote the source map.
- Repository facts cite files at `c80a84e`.
- Decisions that are ours are marked "(ours)".
- Questions for the maintainer are under "Open".

## The constraint: ledgers are signed and chained

Each event carries the hash of the one before it. The signature's manifest carries the SHA-256 of the whole file [repo: `ledger/events.py`, `attest/manifest.py`]. Removing one person's events from a ledger would break both, and the remaining ledger would look the same as a tampered one.

So seatbelt erases **whole ledgers**. Each ledger's `run.start` names one `principal.id`:
- **Gateway:** the issued key's principal, or the identity provider's subject [repo: `gateway/sessions.py`].
- **`seatbelt run` on a machine:** the OS login name, or `uid-<n>` when there is none [repo: `gateway/local.py`].
- **Importer:** the owner's Anthropic user id. It is `unknown` when the API names no user, e.g. a chat whose creator has left an organisation that the key is limited to [repo: `compliance/importer.py`; importer design, C8].

**Limits**, which the docs will state:
- Ledgers under `unknown` cannot be found by id.
- Ledgers written with the SDK `Recorder`, the scenario runner or `demo` carry no principal at all.
- One person's words can appear inside another person's ledger, for example pasted into a colleague's prompt. Those are not found or removed.

## Where a person's data is

| Place | What it holds | Handled by `erase` |
|---|---|---|
| Gateway ledgers, `<ledgers>/*.jsonl` and `*.attest.json` | the person's sessions. `run.start` has `principal.id` [`gateway/sessions.py`], `client.ip` and `client.user_agent` [`gateway/app.py`], the client's `run.name`, and depending on sign-in `principal.key_id`, or `principal.auth`, `principal.issuer` and `principal.name` (the e-mail by default) [`gateway/app.py`, `gateway/config.py`] | yes: deleted |
| Ledger file names | the gateway's run id is `{slug(principal)}-{slug(run) or UTC timestamp}-{8 hex}`, where slug replaces characters outside `[A-Za-z0-9_-]` with `_` and keeps 40 [`gateway/sessions.py`]. So a file name carries the person's id | yes: the files go, and the record never contains a file name or object key |
| `.shipped/<run id>` marks, in each folder a sink ships | the object keys and SHA-256 of each shipped file [`gateway/sink.py`] | yes: deleted |
| Importer ledgers, `<ledgers>/compliance/` | the person's imported conversations, with their Anthropic user id and e-mail in `run.start` [`compliance/importer.py`] | yes: deleted |
| Importer state, `compliance/.state.json` | each conversation's latest list entry, which holds the owner's id and e-mail [`compliance/importer.py`] | yes: the person's entries are removed |
| The sink bucket | a copy of every shipped ledger | listed, not deleted (see "The sink") |
| `gateway.yaml` | issued keys, stored as hashes under principal ids; OIDC `allow` lists of principal ids [`gateway/config.py`] | no: `erase` prints the lines to remove, and does not rewrite the config |
| Gateway and importer logs | ledger file names, in shipping and closing messages [`gateway/sink.py`, `gateway/sessions.py`] | no: the docs say so |
| Evidence packs, backups, copies elsewhere | zip copies of ledgers [`report/pack.py`], and anything outside the folders given | no: the docs say so |

## Nothing may be writing while it runs (ours)

Several things write to these folders, and each one races with a deletion:

- **The gateway** appends to live ledgers. `Ledger.append` creates the file if it is missing [repo: `ledger/store.py`]. A ledger deleted from under a live session would be recreated from mid-chain, with no `run.start` and so no principal, and `erase` could never match it again.
- **The gateway's sink** can have a ledger queued for upload. If the file is gone, the upload skips it and still writes the `.shipped` mark [repo: `gateway/sink.py`].
- **The importer** loads `.state.json` at the start of a run and saves the whole file as it goes [repo: `compliance/importer.py`]. An edit made during an import is overwritten.

So `erase` works on a folder only while it holds that folder's lock:

| Folder | Lock | Held by |
|---|---|---|
| The gateway's `ledgers` | `<ledgers>/.lock` (new) | `gateway serve`, for as long as it runs |
| The importer's folder | `compliance/.lock` | the importer, during a run [repo: `compliance/importer.py`] |
| A local runs folder | the `.running/<run>.lock` of every live run | `seatbelt run` [repo: `gateway/local.py`] |

- **If a lock is held,** `erase` refuses and says what is running. The gateway has to be stopped for the few seconds `erase` takes. The importer and local runs just need to be between runs.
- **Open ledgers.** With the folder locked, nothing is writing, so every open ledger belongs to a process that has died. `erase` removes those as well. It needs no `--include-open`, and cannot catch a live session.

## The command (ours)

```sh
seatbelt erase runs runs/compliance --principal 'auth0|abc123' --principal 'user_01Gp…' \
  --case "DSR-2026-014" --key keys/seatbelt.key
```

- **Who to erase.**
  - `--principal` may be given more than once. Quote it: `|` means something to the shell.
  - Alternatively, `--person "Alice Example" --people people.yaml` takes the person's ids from the 0.4.0 people file.
- **Matching.** A ledger matches when its `run.start` `principal.id` equals one of the ids. Matching on `principal.name` (e-mail) is never done: an address can change and is not always verified [people-and-imported-replay design].
- **Dry run by default.** Without `--yes`, `erase` changes nothing. It prints to the terminal only, and writes nothing:
  - the matching ledgers with their dates and sizes;
  - the `.shipped` marks and the sink object keys;
  - the importer state entries;
  - the config lines to edit.
- **`--case` is required.** It takes free text such as a ticket number, which goes into the record. The docs warn not to put the person's name in it.
- **`--key` is required,** so the record is signed (Open, 1).

## What `--yes` does, and the record it leaves (ours)

A ledger that simply disappears looks the same as one deleted to hide something. So the deletion happens inside a signed record of it, `erasure-<UTC time>-<8 hex>.jsonl`, written in each folder that changes. The file name carries no principal.

In each folder, holding its lock, `erase` does the following:

1. **Opens the record.** Its `run.start` holds:
   - `erasure.case`, the case reference;
   - `erasure.by`, the OS user running `erase`;
   - `principal.id: seatbelt:erasure`, so `seatbelt report` shows erasures as a row of their own.
2. **Writes one `action` event per matching ledger,** holding only the SHA-256 of the ledger, of its sidecar and of its `.shipped` mark, whichever exist. It holds no run id, file name or object key: each of these begins with the person's id (above).
3. **Deletes each ledger,** then its sidecar, then its mark. Then it removes the person's importer state entries, and adds them to the suppression list (below).
4. **Writes an `outcome` event** with the counts actually removed, closes the record and signs it.

**Idempotent.** If the run is killed partway, the record is left open. Because the folder is locked, nothing else closes it in the meantime. The next `erase` in that folder:
1. finds the open record;
2. finds what is left: sidecars and marks by the hashes the record holds, and ledgers by the given principals again;
3. removes them;
4. then closes the record.

**Anyone holding a copy of an erased ledger** can hash it and find that hash in the record. That shows it was removed on purpose, while the record still says nothing about whose it was. Nothing identifying the person is kept, not even a hash of an id: a hash of an id or an address can be reversed by guessing.

**Signing.**
- The record is signed with the key given. For gateway and importer folders, that is the gateway's key, so `verify` and `report` check it like the other ledgers there.
- A local runs folder is signed by its machine's own key [repo: `gateway/local.py`], so an erasure there is signed with the key given, which may differ.
- In a local runs folder, the record becomes the "latest run" that `verify` and `reconstruct` pick with no argument.

**Shipping.** A sink ships the record like any closed ledger. It holds hashes, the case, `erasure.by` and counts, and nothing about the person.

## Keeping the importer from bringing it back (ours)

Without a guard, the importer's next run would list the person's conversations again and re-import them, because Anthropic still keeps them:
- **local sessions** for 6 years by default, or for the organisation's custom retention period;
- **remote sessions** for 6 years;
- **chats** under the organisation's own retention policy [importer design, "Retention"].

- **The suppression list.** `erase` adds `sha256(id)` for each erased id to `compliance/.erased`, a file of its own, and the importer skips any conversation whose owner's id hashes to one of them.
- **Not in `.state.json`.** The importer treats that file as disposable: when it cannot be read, it starts again [repo: `compliance/importer.py`]. The suppression list must not be lost the same way.
- **An unreadable `.erased` stops the import** rather than being ignored.
- **The trade-off.** Anyone who already knows an id can confirm it is on the list. That is accepted, because the list's purpose is to stop re-import (Open, 2).

## The sink (ours)

The gateway guide asks for the sink bucket to be created with Object Lock in COMPLIANCE mode. It says "a shipped ledger's version cannot be deleted before its retention ends, by the gateway's key or anyone else's", and "COMPLIANCE mode cannot be ended early" [repo: `docs/deploy/gateway.md`]. In Amazon S3's own documentation, a retention period "protects only the version that's specified", and does not prevent "delete markers to be added on top of the object" [S1]. So a plain DELETE of a locked object's key would appear to succeed while the copy stays.

Hetzner's behaviour here has not been checked. The gateway guide describes Hetzner as S3-compatible [NEEDS SOURCE for its delete-marker semantics].

So in the first version, `erase`:
- prints the object keys of each ledger it removes, in the dry run and after deleting;
- deletes nothing from the bucket (Open, 3).

The docs will say plainly that a bucket under COMPLIANCE retention keeps its copy until the retention ends. An organisation that must be able to erase should choose its retention with that in mind.

## Testing (ours)

- **Matching.** In a folder with two people's gateway ledgers, an importer folder and a dead open ledger, `erase` removes only the given person's ledgers, sidecars and marks. Ledgers under `unknown`, or with no principal, are left alone and reported.
- **Dry run:** changes nothing and writes nothing.
- **Locks:** a held `<ledgers>/.lock`, `compliance/.lock` or live `.running` lock makes `erase` refuse.
- **The record:**
  - it verifies and is signed;
  - its file name and content hold only hashes, the case, `erasure.by` and counts;
  - a byte search for each erased id, its slug and its e-mail across every file in the folders afterwards finds nothing.
- **Interrupted run:** killed after two of five deletions, the next run finishes and closes the record.
- **Importer:**
  - the next import skips the erased owner's conversations;
  - an unreadable `.erased` stops the import.
- **Report:** `seatbelt report` no longer counts the person, and shows the `seatbelt:erasure` row.

## Decided (2026-09-29)

1. **Signing key: required.** `--yes` needs `--key`, or `--config` with a signing key.
2. **Suppression list: kept.** It is `compliance/.erased`. An unreadable list stops the import.
3. **Sink: list only.** The object keys are printed, and nothing is deleted from the bucket.
4. **`seatbelt search`: not now.** Other people's ledgers that mention the person are a stated limit.
5. **Gateway lock: added.** `gateway serve` holds `<ledgers>/.lock` while it runs.

## Source map

| Ref | Source | Used for |
|---|---|---|
| G1 | Regulation (EU) 2016/679 (GDPR), Article 17, "Right to erasure ('right to be forgotten')". Read 2026-09-29 in two copies: as reproduced at gdpr-info.eu/art-17-gdpr, and in the UK GDPR at legislation.gov.uk/eur/2016/679/article/17, whose 17(3)(b) reads "…under domestic law". | "without undue delay" (17(1)); the legal-obligation (17(3)(b)) and legal-claims (17(3)(e)) exceptions |
| S1 | Amazon S3 User Guide, "Locking objects with Object Lock", docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html, read 2026-09-29 | retention "protects only the version that's specified in the request" and does not prevent "delete markers to be added on top of the object"; in compliance mode "a protected object version can't be overwritten or deleted by any user" |
| repo | at `c80a84e`: `ledger/events.py`, `ledger/store.py`, `attest/manifest.py`, `gateway/sessions.py`, `gateway/app.py`, `gateway/config.py`, `gateway/local.py`, `gateway/sink.py`, `compliance/importer.py`, `report/pack.py`, `docs/deploy/gateway.md`; `docs/plans/2026-09-29-compliance-importer-design.md` (retention; C8); `docs/plans/2026-09-29-people-and-imported-replay.md` | where a person's data is, what writes it, and why whole ledgers |
