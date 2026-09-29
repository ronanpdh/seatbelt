# 1. Hash-chained JSONL ledger per run

- Status: accepted
- Date: 2026-09-14

## Context

Seatbelt exists to give a reviewer a record of an agent run that is attributable (who did each thing), reconstructable (what happened, in order) and provable (the record has not been changed since it was written).

The record has to be written by the process being observed, on the host's own disk, with no database or service to depend on. A reviewer must be able to check it with nothing but the file and the harness.

## Decision

Each run is one append-only JSONL file, one event per line.

Every event carries `prev_hash`, the SHA-256 of the previous event, and `hash`, the SHA-256 of its own canonical JSON (sorted keys, no whitespace, every field except `hash`). The first event links to a genesis hash of 64 zeros. Events also carry a `seq` counter starting at 0.

Redaction runs before an event is hashed or written, so the stored, provable content never contains the secrets it scrubbed. It covers dict keys as well as values (two keys that redact to the same text are kept apart with a `#2` suffix), and the ledger itself redacts the actor's id and version and the parent id, so no caller can skip it.

Every lone UTF-16 surrogate in an event (in a string, a dict key, the actor or the parent id) is replaced by its escape as visible text, for example `\ud800`, before the event is hashed. JSON hashing accepts a lone surrogate but UTF-8 cannot write it, and an event that can be hashed but not written would be lost.

Verification walks the file and fails on the first event whose `seq` is out of order, whose `prev_hash` does not match, or whose content does not match its hash.

## Consequences

- Any edit, reorder, insertion or deletion inside the chain is detected and located by sequence number.
- Removing events from the end of the file is caught only by a completeness check: `verify` expects a final `run.end` whose `run.events` count matches. Anyone able to rewrite the file can forge that too, so closing the gap needs the final hash recorded somewhere the writer cannot rewrite, which is the job of signed attestation (ADR 0002).
- JSONL can be read with standard tools and appended without rewriting the file, and a run can resume after a restart by reading the last hash.
- The line is serialised before the file is opened. An append whose write, flush or fsync fails is truncated back to the file's previous size and the error re-raised, so the next append does not fuse onto a partial line; if even the truncation fails, that `Ledger` refuses further appends. Only a crash (power loss, kill) halfway through a line can still leave an invalid final line. The ledger then fails to load and the run cannot resume until the line is removed.
- The canonical form is tied to the Pydantic model. Changing a field changes every hash, so schema changes need a version bump. The models forbid unknown keys: the canonical form is a re-dump, so an ignored extra key would otherwise survive verification.
- Each append is fsynced and the file is created mode 0600; the directory is fsynced once when the file is created, so the new name survives power loss too. Durability and confidentiality cost a syscall per event, which is nothing at agent speeds.
- `Recorder.start` refuses a file that already exists. Resuming a run is the ledger's job (it reads the last hash), not the recorder's; a second `run.start` in one file is reported as incomplete.
