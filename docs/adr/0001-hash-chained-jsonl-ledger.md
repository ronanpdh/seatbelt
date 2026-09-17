# 1. Hash-chained JSONL ledger per run

- Status: accepted
- Date: 2026-09-14

## Context

Seatbelt exists to give a reviewer a record of an agent run that is attributable (who did each thing), reconstructable (what happened, in order) and provable (the record has not been changed since it was written).

The record has to be written by the process being observed, on the host's own disk, with no database or service to depend on. A reviewer must be able to check it with nothing but the file and the harness.

## Decision

Each run is one append-only JSONL file, one event per line.

Every event carries `prev_hash`, the SHA-256 of the previous event, and `hash`, the SHA-256 of its own canonical JSON (sorted keys, no whitespace, every field except `hash`). The first event links to a genesis hash of 64 zeros. Events also carry a `seq` counter starting at 0.

Redaction runs before an event is hashed or written, so the stored, provable content never contains the secrets it scrubbed.

Verification walks the file and fails on the first event whose `seq` is out of order, whose `prev_hash` does not match, or whose content does not match its hash.

## Consequences

- Any edit, reorder, insertion or deletion inside the chain is detected and located by sequence number.
- Removing events from the end of the file is caught only by a completeness check: `verify` expects a final `run.end` whose `run.events` count matches. Anyone able to rewrite the file can forge that too, so closing the gap needs the final hash recorded somewhere the writer cannot rewrite, which is the job of signed attestation (ADR 0002).
- JSONL can be read with standard tools and appended without rewriting the file, and a run can resume after a restart by reading the last hash.
- A write interrupted halfway through a line leaves an invalid final line. The ledger then fails to load and the run cannot resume until the line is removed.
- The canonical form is tied to the Pydantic model. Changing a field changes every hash, so schema changes need a version bump. The models forbid unknown keys: the canonical form is a re-dump, so an ignored extra key would otherwise survive verification.
- Each append is fsynced and the file is created mode 0600. Durability and confidentiality cost a syscall per event, which is nothing at agent speeds.
- `Recorder.start` refuses a file that already exists. Resuming a run is the ledger's job (it reads the last hash), not the recorder's; a second `run.start` in one file is reported as incomplete.
