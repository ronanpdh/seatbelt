# Erasing a person's ledgers

`seatbelt erase` removes every ledger recorded under a person's principal ids, together with each ledger's signature, shipped mark and HTML page. It leaves a signed record that the removal happened. It never edits a ledger: ledgers are hash-chained and signed, so it removes whole ones. Design and sources: [docs/plans/2026-09-29-erasure-design.md](../plans/2026-09-29-erasure-design.md).

**Whether to erase is your decision.** The GDPR's right to erasure has exceptions, for example where processing is necessary to comply with a legal obligation, or for legal claims (Article 17(3)(b) and (e)). seatbelt only carries out the decision, and records your case reference.

## Before you start

**Stop what writes to the folders.** `erase` refuses to run while anything is writing to them:
- **Gateway:** stop `gateway serve`. It holds `<ledgers>/.lock` while it runs.
- **Importer:** wait until no import is running. An import holds `compliance/.lock`.
- **Local runs:** wait until no `seatbelt run` is recording into the folder.

**Know the person's principal ids.** These are:
- the id of the key you issued them, e.g. `alice@corp`;
- their identity provider's subject, e.g. `auth0|abc123` (in `run.start` as `principal.id`);
- their Anthropic user id, if you import from the Compliance API.

A people file (`seatbelt report --people`) can hold all of these.

## Run it

First, a dry run. It changes nothing, and lists what would go:

```sh
seatbelt erase runs runs/compliance --person "Alice Example" --people people.yaml \
  --case "DSR-2026-014" --config gateway.yaml
```

- **Ids directly:** instead of `--person`, give each id with `--principal`, and quote ids that contain `|`: `--principal 'auth0|abc123'`.
- **What it lists:** each ledger and its size, and each ledger's copy in the sink, which `erase` does not delete.
- **With `--config`:** it also lists the lines in `gateway.yaml` that name the person, such as their issued key or an `oidc.allow` entry. Remove those yourself.

Then erase, with `--yes`. Signing the record needs `--key`, or `--config` with a signing key:

```sh
seatbelt erase runs runs/compliance --person "Alice Example" --people people.yaml \
  --case "DSR-2026-014" --config gateway.yaml --yes
```

Start the gateway again afterwards.

## What it does

- **Removes** each matching ledger, its `.attest.json` signature, and its `.shipped` mark.
- **Importer folders:**
  - removes the person's entries from `.state.json`;
  - adds a hash of each id to `.erased` (mode 0600), so the importer never imports that person's conversations again, even though Anthropic still keeps them. The hashes are unsalted: anyone who can read the folder can check whether a given id, an email address say, was erased. If `.erased` cannot be read, the import stops rather than risk bringing them back.
- **Writes a signed record** in each folder it changes: `erasure-<time>-<hex>.jsonl`.
  - It holds a SHA-256 hash of every file removed, your case reference, who ran `erase`, and the counts.
  - It holds no file name or id, because gateway file names begin with the person's id.
  - Anyone holding a copy of an erased ledger can hash it and find it in the record, but the record does not say whose it was.
  - `seatbelt verify` checks the record, and `seatbelt report` shows erasures as a `seatbelt:erasure` row.
- **Finishes interrupted erasures.** If an erase is interrupted, run `erase` again: it removes anything a record names that is still there.
- **Leaves unreadable ledgers for you.** A ledger it cannot read (a line torn by a crash) is not removed. When its first line names the person, or cannot be read, the listing shows it as `unreadable, may be theirs, not erased`, and `erase --yes` erases everything else, then names each such file by its path and exits 1. Repair or remove those by hand.

## What it cannot reach

- **The sink bucket.** With Object Lock in COMPLIANCE mode, a stored version cannot be deleted before its retention ends, by anyone. `erase` lists the object keys; the copies stay until their retention ends. Choose the bucket's retention with erasure in mind.
- **Ledgers with no person, or `unknown`.** These are listed, not searched:
  - ledgers written with the SDK `Recorder`, the scenario runner or `demo`;
  - imported conversations whose owner the API does not name.
- **The person in someone else's ledger,** for example text they sent a colleague who pasted it into a prompt.
- **Copies outside the folders you give it:**
  - evidence packs (`seatbelt pack`);
  - backups;
  - logs, which print ledger file names;
  - local runs on employees' machines, which are keyed by their login name.
