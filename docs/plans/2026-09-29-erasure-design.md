# Removing one person's ledgers on request (0.4.0): design

**Goal:** `seatbelt erase` removes every ledger a given person made, from the folders seatbelt manages, and leaves a signed record that the removal happened. It never edits a ledger.

**What it is not.** It does not decide whether a request must be honoured. Whether data must be erased is the controller's decision. The law names cases where erasure does not apply, among them processing needed "for compliance with a legal obligation" and "for the establishment, exercise or defence of legal claims" [G1, Art. 17(3)(b) and (e)]. A legal hold is the usual reason an organisation keeps these ledgers. seatbelt provides the mechanism and asks for the case reference; the decision stays with the organisation.

**Status:** a design for review. Every legal statement quotes its source in the source map. Repository facts cite files at `c80a84e`. Decisions that are ours are marked "(ours)", and questions for the maintainer are under "Open".

## The constraint: ledgers are signed and chained

Each ledger is a hash chain, and its signature covers the whole file [repo: `ledger/events.py`, `attest/`]. Removing one person's events from a shared ledger would break the chain and the signature. That would make the rest of the ledger unverifiable, and indistinguishable from tampering.

So seatbelt erases **whole ledgers**. This works because every ledger belongs to one person: its `run.start` names one `principal.id`, whether it was recorded by the gateway, by `seatbelt run` on a machine, or by the importer [repo: `gateway/sessions.py`, `compliance/importer.py`].

**The limit.** One person's words can also appear inside another person's ledger, for example when a colleague pastes their message into a prompt. Those are not found or removed. The docs will say so.

## Where a person's data is

| Place | What it holds | Handled by `erase` |
|---|---|---|
| Gateway ledgers, `<ledgers>/*.jsonl` and `*.attest.json` | the person's sessions; `run.start` has `principal.id`, and may have `principal.name` (e-mail), `client.ip` and `client.user_agent` [repo: `gateway/app.py`] | yes: deleted |
| Gateway run ids and file names | the gateway's run id begins with the principal id, slugged: `{slug(principal)}-{run}-{hex}` [repo: `gateway/sessions.py`] | yes: the files go, and the erasure record does not name them (below) |
| `<ledgers>/.shipped/<run id>` | a mark per shipped ledger, with its object keys and SHA-256 [repo: `gateway/sink.py`] | yes: deleted |
| Importer ledgers, `<ledgers>/compliance/` | the person's imported conversations, with their Anthropic user id and e-mail in `run.start` [repo: `compliance/importer.py`] | yes: deleted |
| Importer state, `.state.json` | each conversation's latest list entry, including the owner's id and e-mail [repo: `compliance/importer.py`] | yes: the person's entries are removed |
| The sink bucket | a copy of every shipped ledger | see "The sink" below |
| `gateway.yaml` | the person's issued key, stored as a hash under their principal id; OIDC `allow` lists [repo: `gateway/config.py`] | no: `erase` prints which lines to remove. It does not rewrite the config. |
| Evidence packs (`seatbelt pack`) | zip files that contain copies of ledgers [repo: `report/pack.py`] | no: listed in the docs as copies to find |
| Backups, and copies on laptops | anything outside the folders given | no: the docs say so |

## The command (ours)

```sh
seatbelt erase runs runs/compliance --principal auth0|abc123 --principal user_01Gp… \
  --case "DSR-2026-014" --key keys/seatbelt.key
```

- **Who to erase.** `--principal` may be given more than once. Alternatively, `--person "Alice Example" --people people.yaml` takes the ids from the people file from 0.4.0.
- **Dry run by default.** Without `--yes`, `erase` only lists what it would remove: the ledgers with their dates and sizes, the `.shipped` marks, the importer state entries, the sink objects, and the config lines to edit. It changes nothing.
- **What `--yes` does:**
  1. Refuses to start if any matching ledger is still open, meaning it has no `run.end`. A live session is still being written, so it must be closed first (`gateway serve` closes idle sessions). An open ledger left by a crash can be erased with `--include-open`.
  2. Writes the erasure record first, and signs it.
  3. Deletes each ledger, then its sidecar, then its `.shipped` mark.
  4. Removes the importer state entries.

  If the run stops partway, the record already names every file it meant to remove, so running `erase` again finishes the job.
- **Whose ids count.** A ledger matches when its `run.start` `principal.id` equals a given id. Matching on `principal.name` (e-mail) is never done, for the reason given in the people-file design: an address can change and is not always verified.
- **Case reference.** `--case` is required, with free text such as a ticket number. It goes into the record. The docs warn not to put the person's name in it.

## The erasure record (ours)

A deleted ledger simply vanishes, which looks the same as a ledger deleted to hide something. So `erase` leaves evidence of what it did: its own signed ledger, `erasure-<UTC time>-<hex>.jsonl`, in each folder it changed.

- `run.start` carries:
  - `erasure.case`: the case reference;
  - `erasure.by`: the OS user who ran it;
  - `principal.id: seatbelt:erasure`, so `seatbelt report` shows erasures as their own row.
- **One `action` event per removed ledger.** It records the ledger's SHA-256, and the SHA-256 in its signature if it had one, but not the run id or the file name. The gateway's run ids begin with the person's id, so naming them would keep exactly what was erased. A hash lets anyone holding a copy check that it is one of the erased ledgers, without the record saying whose it was.
- **One `outcome` event** records the counts: ledgers, sidecars, marks and state entries removed.
- **Nothing identifying the person.** The principal ids are not in the record, not even hashed. A hash of an id or an e-mail can be reversed by guessing.
- The record is signed with the same key as the ledgers, so `verify` and `report` check it like any other.

## Keeping the importer from bringing it back (ours)

Anthropic keeps local-session transcripts for 6 years by default (see the importer design). Without a guard, the importer's next run would list the person's conversations again and re-import them.

- `erase` adds the given ids to `erased` in the importer's state, as `sha256(id)`.
- The importer then skips any conversation whose owner's id hashes to one of them.

The hash is a trade-off. It lets the importer recognise the person without keeping their id in the clear. Anyone who already knows the id can confirm it is on the list. That is accepted, because the purpose is to stop re-import (Open, 2).

## The sink (ours)

The gateway guide tells operators to create the sink bucket with Object Lock in COMPLIANCE mode. In that mode "a shipped ledger's version cannot be deleted before its retention ends, by the gateway's key or anyone else's", and the retention cannot be shortened [repo: `docs/deploy/gateway.md`, "Shipping ledgers to object storage"].

So the sink copy cannot be deleted before its retention ends, by seatbelt or anyone else. `erase` therefore:
- lists the object keys of each ledger it removes, taken from the `.shipped` marks, in both the dry run and the record's outcome;
- deletes nothing from the bucket in the first version (Open, 3).

The docs will say plainly that a bucket under COMPLIANCE retention keeps its copy until the retention ends. An organisation that must be able to erase should choose its retention with that in mind, since COMPLIANCE mode cannot be ended early.

## Testing (ours)

- **Matching:** in a folder with two people's gateway ledgers, imported ledgers and an open ledger, `erase` removes only the given person's closed ledgers, sidecars and marks.
- **Dry run:** changes nothing.
- **Open ledgers:** an open ledger makes `--yes` refuse, unless `--include-open` is given.
- **The record:**
  - it verifies and is signed;
  - it holds only hashes, the case and the counts;
  - a grep for the person's ids and e-mail over the whole folder afterwards finds nothing.
- **Interrupted run:** a run stopped after the record is written finishes when run again.
- **Importer:**
  - the next import skips the erased owner's conversations;
  - its state holds no entry naming them.
- **Report:** `seatbelt report` no longer counts the person, and shows the `seatbelt:erasure` row.

## Open

1. **Should `erase` be allowed at all without a signing key?** Without one the record cannot be signed, only written. Recommendation: require the key, as the gateway does.
2. **The importer's suppression list.** It stores `sha256(id)` for every erased person, indefinitely. The alternative is to store nothing, and accept that the next import brings the conversations back while Anthropic still keeps them. Recommendation: keep the list.
3. **Deleting from the sink.** The first version lists the objects only. A later version could try an S3 `DELETE` for each object and report what the bucket refused. Under COMPLIANCE retention that is every object, so this only helps buckets without Object Lock. Recommendation: list only, for now.
4. **Other people's ledgers that mention the person.** They are out of reach without editing ledgers, and the docs say so. Is a search command (`seatbelt search <text>`) worth adding, so an operator can at least find them?

## Source map

| Ref | Source | Used for |
|---|---|---|
| G1 | Regulation (EU) 2016/679 (GDPR), Article 17 "Right to erasure ('right to be forgotten')", as reproduced at gdpr-info.eu/art-17-gdpr, and the UK GDPR text at legislation.gov.uk/eur/2016/679/article/17, both read 2026-09-29 | the right to erasure "without undue delay" (17(1)); the exceptions for compliance with a legal obligation (17(3)(b)) and for legal claims (17(3)(e)) |
| repo | at `c80a84e`: `ledger/events.py`, `attest/`, `gateway/sessions.py` (run id format), `gateway/app.py` (`client.ip`, `client.user_agent`), `gateway/sink.py` (`.shipped` marks), `gateway/config.py`, `compliance/importer.py` (state, run ids), `report/pack.py` (packs copy ledgers), `docs/deploy/gateway.md` (Object Lock COMPLIANCE) | where a person's data is; why whole ledgers |
