# Importing from the Compliance API

`seatbelt import compliance` copies the transcripts Anthropic keeps for a **Claude Enterprise** organization into signed seatbelt ledgers [C1]. It covers what a gateway cannot see:
- claude.ai chats;
- Cowork, on the desktop and in the cloud;
- Claude Code, Claude for Microsoft 365, Claude Science and Claude in Chrome sessions on machines that do not route through a gateway [C2].

> **Not yet run against a live tenant.** The importer is built and tested against a fake of the API as Anthropic documents it. Run it against a test organization first, and please report what differs.

## What an imported ledger proves

A gateway ledger records the bytes that crossed the gateway. An imported ledger records **what the Compliance API returned when seatbelt asked**:
- `run.start` names the source (`compliance.source`) and the endpoint and query it came from.
- Every event carries the API's message id and timestamp.
- An event's own `ts` is when seatbelt imported it.
- The signature proves the ledger is unchanged since then.

The transcript is Anthropic's reconstruction, so it has Anthropic's gaps. It has no thinking, no system prompts for local sessions, no tool definitions, no images or other binary content, and no token counts [C2][C7]. Messages Anthropic marks as unverified or unavailable are recorded with that mark (`compliance.provenance`). `seatbelt report` does not count them as model calls.

## Set up

1. **Create a key.** The primary owner enables the Compliance API and creates a Compliance Access Key in claude.ai > Organization settings > API [C4].
   - Give it `read:compliance_user_data` only. The importer never deletes anything, so it needs no delete scope.
   - Store the key in a secrets manager.
2. **Put it in the environment** where the importer runs, as `ANTHROPIC_COMPLIANCE_ACCESS_KEY`, or under the name set in `key_env`. It is never read from the config file, and never logged or recorded.
3. **Add a `compliance:` block** to the gateway's config file. The importer signs with the gateway's key, so one public key checks both kinds of ledger. A file only for the importer needs just `ledgers`, a signing key and this block.

```yaml
signing_key: keys/seatbelt.key    # or SEATBELT_SIGNING_KEY in the environment
ledgers: runs
compliance:
  # every setting is optional; these are the defaults, except since and surfaces
  key_env: ANTHROPIC_COMPLIANCE_ACCESS_KEY
  ledgers: runs/compliance         # default: compliance/ inside `ledgers`
  sources: [local_sessions, remote_sessions, chats]
  surfaces: [cowork, cowork_remote] # default: every product
  since: 2026-09-01T00:00:00Z      # first run only; default: all Anthropic still keeps
  settle: 3600                     # seconds a conversation must be quiet before import
  overlap: 900                     # seconds each run looks back, for late-indexed sessions
```

4. **Run it on a schedule**, for example every 15 minutes from cron, a systemd timer or a Coolify scheduled task:

```sh
seatbelt import compliance --config gateway.yaml
```

Each run imports what changed and settled since the last run, then prints what it did: per source, how many conversations were listed and how many ledgers and messages were written, plus the last `request-id`. It exits 1 when a conversation needs a person to look at it. Two runs never overlap on one folder: the second exits with an error.

**`surfaces`** stops an org that routes Claude Code through the gateway from recording it twice. With `[cowork, cowork_remote]`, the gateway keeps Claude Code and the importer takes Cowork. The values are the API's `product_surface` names:
- `cowork`
- `claude_code`
- `claude_science`
- `claude_in_chrome`
- `office_agents/excel`, `office_agents/word`, `office_agents/powerpoint`, `office_agents/outlook`
- `cowork_remote` [C2]

## How conversations become ledgers

- **When.** A conversation is imported once it has been quiet for `settle` (1 hour); a cloud session also counts as quiet once it is archived or failed. Its ledger is named `<product>-<id>-1`, for example `cowork-clls_01Hx...-1`, and chats are `chat-<id>-1`.
- **Growth.** If the conversation continues, the next import writes `...-2` with only the new messages. Its `run.start` names the previous ledger and its SHA-256 (`compliance.previous`).
- **Deleted chats.** A chat deleted in claude.ai gets one more ledger with no messages, recording when it was deleted (`compliance.deleted_at`).
- **Aged-out turns.** When Anthropic's retention removes the oldest turns, the ledgers already written keep them.
- **Following cloud sessions.** A cloud session is followed until it is archived, failed or deleted, or has had no activity for 30 days. Later activity in one that was dropped for no activity is imported only if it shows up in a later listing.
- **Where the state lives.** State is kept in `.state.json` in the importer's folder. If it is lost, the next run rebuilds it from the ledgers and the API, and records nothing twice.
- **Erased people.** `seatbelt erase` adds a hash of each erased id to `.erased` in the importer's folder. The importer then never imports those people's conversations again. If `.erased` cannot be read, the import stops ([erasing a person's ledgers](erasure.md)).
- **A killed run.** If a run is killed mid-ledger, the next run closes that ledger as failed ("importer killed"), signs it, and carries on after its last message.

Read the ledgers with the usual commands, passing the importer's folder:

```sh
seatbelt report runs runs/compliance --pubkey keys/seatbelt.pub --people people.yaml
seatbelt reconstruct runs/compliance/cowork-clls_01Hx...-1.jsonl --pubkey keys/seatbelt.pub
```

`reconstruct` shows each imported answer's text. A message Anthropic marks is shown with its mark: `[unverified]` for a turn the client claimed, `[marker]` for a placeholder such as the system prompt's, and `[unavailable: <reason>]` for content that could not be returned.

With `sink:` in the config, each ledger is also shipped to object storage as it is written. The gateway's own sink does not ship the importer's folder.

## Limits and cautions

- **Enterprise only.** Chats and session transcripts are served only for Claude Enterprise organizations. A Claude Console organization can read only the Activity Feed, which has no message content [C1][C7].
- **Not everything reaches the API** [C2][C6]:
  - Claude Code with a Console API key, or through Amazon Bedrock, Google Cloud or Microsoft Foundry;
  - Claude Code cloud sessions;
  - local sessions in HIPAA-ready organizations, or under zero data retention;
  - content already removed by retention or deleted by a user before an import.
- **Whole tool blocks, up to a point.** The importer asks for tool inputs and results up to the server's maximum, about 1 MiB [C2]. A block still cut is recorded with `compliance.truncated`, its input kept as a string.
- **Files and artifacts: metadata only.** Chat attachments, generated files and artifacts are recorded as ids, names, types, and where given, sizes and MD5s. Their content is not downloaded.
- **People.** Imported ledgers name people by Anthropic's user id (`principal.id`), with their email as `principal.name`. Gateway ledgers name them by issued key or OIDC subject. To count each person once, list their ids in a people file and pass it to `seatbelt report --people` ([gateway guide](gateway.md#one-row-per-person)).
- **Deleted content is kept.** A signed ledger keeps what a user later deletes in claude.ai. That is the point of a legal hold, but it can conflict with data-protection duties such as erasure requests. Decide how you will handle those before you start. `seatbelt erase` removes a person's ledgers and leaves a signed record of it; run on the importer's folder, it also stops the importer bringing their conversations in again. It does not delete copies already shipped to object storage ([erasing a person's ledgers](erasure.md)).
- **Rate limit.** The Compliance API allows 600 requests per minute per parent organization, shared by every key [C1][C5]. The importer slows down when fewer than 30 are left in the minute, and honours `retry-after`.
- **Cursors expire.** A walk of local-session messages must finish within 24 hours [C2]. A run fetches each transcript in one go.

## Sources

The refs are those of the design, [docs/plans/2026-09-29-compliance-importer-design.md](../plans/2026-09-29-compliance-importer-design.md), whose source map gives each page and what was taken from it: C1 overview, C2 session transcripts, C4 set-up, C5 errors, C6 integration patterns, C7 FAQ.
