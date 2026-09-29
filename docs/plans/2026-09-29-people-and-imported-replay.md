# One row per person, and readable imported replay (0.4.0)

Two small changes, both in how recorded ledgers are read. Neither changes what is recorded.

## One row per person

**Problem.** `seatbelt report` groups usage by `principal.id`. One person can have several:
- an issued gateway key;
- an identity provider's subject (`principal.auth: oidc`);
- an Anthropic user id, from the Compliance API importer, which the importer design deferred ([2026-09-29-compliance-importer-design.md](2026-09-29-compliance-importer-design.md), Decided 1).

So one person shows up as several rows.

**Decision.** A people file, passed to `report` with `--people`:
- `people:` maps each person's name to a list of principal ids.
- The mapping is applied when the report is built, never when a ledger is recorded. The ledgers and their signatures stay as recorded, and a mistake in the file is fixed by editing the file.
- **Explicit ids only; nothing is joined by e-mail.** The identity-provider guidance followed for OIDC keys users on the provider's immutable id, because an e-mail address can change or be absent ([deploy/claude-desktop-gateway.md](../deploy/claude-desktop-gateway.md), sources 1 and 4). Some providers also do not verify e-mail, so matching on it would let one person's usage be counted as another's.
- An id listed for two people is refused.
- `report` also takes several folders, so that the gateway's ledgers and the importer's (`compliance/`) are counted together.
- The report lists, per person, the ids it joined, so a reader can check the grouping (`people` in `--json`).

**Not done.** Ids are not scoped by issuer. Two identity providers that issue the same `sub` would be joined if both ids were listed for one person. The file lists ids, so that is the author's choice.

## Readable imported replay

**Problem.** `reconstruct` described every `model.response` by its model and output token count. Imported answers have no token counts (the Compliance API does not return them), so each showed `(? out)` and none of its text.

**Decision.**
- An imported answer (one with `compliance.message_id`) is shown as its model and text.
- Imported messages that carry `compliance.provenance` are marked before their text:
  - `[unverified]` for a turn the client asserted;
  - `[marker]` for a placeholder such as the system prompt's;
  - `[unavailable: <reason>]` for content the API could not return.
- Gateway answers are shown as before.
