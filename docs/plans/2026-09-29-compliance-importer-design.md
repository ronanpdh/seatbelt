# Compliance API importer (0.3.0): design

**Goal:** `seatbelt import compliance` pulls the transcripts Anthropic keeps for a Claude Enterprise organization into signed seatbelt ledgers. It covers what the gateway cannot see: claude.ai chats, Cowork (on the desktop and in the cloud), and Claude Code, Claude for Microsoft 365, Claude Science and Claude in Chrome sessions on machines whose traffic is not routed through a gateway.

**What a ledger from the importer attests.** A gateway ledger says "these bytes crossed the gateway". An imported ledger says "this is what Anthropic's Compliance API returned when seatbelt asked, at this time". The transcript is Anthropic's reconstruction, with gaps Anthropic documents (below), so every imported ledger names its source and every event carries the API's own ids and timestamps. The seatbelt signature proves only that the ledger has not changed since the import.

**Status:** a design for review. Every Compliance API fact below comes from the sources in the source map at the end, read on 2026-09-29. Decisions that are ours, not the API's, are marked "(ours)". The maintainer's decisions on the open questions are under "Decided".

## Scope

**Who can use it.** Transcript content (chats and sessions) is served for Claude Enterprise organizations only [C1][C6]. A standalone Claude Console organization can read the Activity Feed only, which carries no prompt or response text [C1][C7]. The roadmap's "Team and Enterprise" is therefore wrong: this change corrects it to Enterprise.

**Imported, first version (ours):**

| Source | Endpoints | Covers |
|---|---|---|
| Local sessions | `GET /v1/compliance/apps/sessions/local`, `/{id}`, `/{id}/messages` [C2] | Cowork in Claude Desktop, Claude Code (terminal, desktop, IDE), Claude Science, Claude for Microsoft 365, Claude in Chrome, on the user's machine [C2] |
| Remote sessions | `GET /v1/compliance/apps/sessions/remote`, `/{id}/messages` [C2] | Cowork started on claude.ai web or mobile, run in the cloud [C2] |
| Chats | `GET /v1/compliance/apps/chats`, `/{id}/messages` [C3] | claude.ai chats |

**Not imported, first version (ours):**
- File, generated-file and artifact *content*. The chat messages carry their ids, names, sizes and MD5s [C3][C8], and those are recorded. Downloading the bytes is a later change.
- Projects and project attachments.
- The Activity Feed. It has no message content [C7].
- Deletes. The importer never calls a `DELETE` endpoint.

**Never in the API, so never in an imported ledger** [C2][C6][C7]:
- thinking blocks;
- system prompts of local sessions (a marker stands in);
- tool definitions and MCP configuration;
- images and other binary content (a placeholder stands in);
- token usage and cost;
- Claude Code with a Console API key, through Bedrock, Vertex AI or Foundry, or in Claude Code cloud sessions;
- local sessions in HIPAA-ready organizations or under zero data retention;
- content already removed by retention or deleted by a user.

## Key and access

- **Key:** a Compliance Access Key (`sk-ant-api01-...`), made in claude.ai > Organization settings > API by the primary owner (for the whole parent organization) or an organization owner (for their organization) [C4]. Admin API keys get 403 on the content and session endpoints [C2][C3].
- **Scope:** `read:compliance_user_data` covers chats, messages, session metadata and transcripts [C4]. The docs tell readers to give a read-only integration no `delete:compliance_user_data` [C4]. The importer's docs say the same. The importer cannot check the key's scopes up front; a call beyond them returns 403 with the scopes the key has [C4].
- **Where it is read:** from an environment variable, never the config file (ours, as for the sink's credentials). The default is `ANTHROPIC_COMPLIANCE_ACCESS_KEY`, the name Anthropic's examples use [C4]. The key is never logged or recorded.
- **Every request** goes to `https://api.anthropic.com/v1/compliance/...` with `x-api-key` and `anthropic-version: 2023-06-01` [C1].

## Pulling: what changed since the last run

Each source is walked differently, because the API gives each different filters and cursors.

**Local sessions** [C2]:
- The list has no user filter. It takes `created_at.gte`, `created_at.lt` and `updated_at.gte`, and pages forward with `page` and `next_page` (newest first, `limit` up to 500) until `next_page` is `null`.
- To find sessions active since the last run, pass `updated_at.gte`. Anthropic says to set it "a few minutes before your previous run's start time", because a session's last call can still be indexing. A bound set exactly at the previous run can drop a session for good. The importer uses the previous run's start minus 15 minutes (ours; `overlap` in the config).
- A list walk should finish within 24 hours of its first page.

**Remote sessions** [C2]:
- The list has no `updated_at` filter, only `created_at` bounds. It pages with `page` and `next_page`, newest first.
- Each session has a `status`: `pending`, `active`, `paused`, `archived` or `failed`. A `pending` session has no transcript yet, and its messages return 404.
- The importer re-lists by `created_at` back to the oldest session it still holds as not final (ours). It fetches the messages of every listed session it has not finished.

**Chats** [C3]:
- The organization-wide list with `order_by=updated_at` pages oldest first, with `after_id` set to the previous page's `last_id`, until `has_more` is false.
- Saving the final `last_id` and resuming from it is Anthropic's recommended way to keep an export current. A chat reappears after the cursor when it gets a new message, moves in or out of a project, or is deleted in claude.ai. A rename may not bring it back.
- A chat that reappears with `deleted_at` set has no content left.

**Transcripts** [C2][C3]:
- Both session families: oldest first, paged with `page` and `next_page` (`limit` up to 1,000). A page can be short without being the last, so the walk goes on until `next_page` is `null`. Messages cursors expire 24 hours after a walk's first page.
- Chat messages: the whole chat comes in one response when `limit` is left out [C8]. The importer leaves it out.
- **Truncation:** tool inputs and tool results are cut at 10,000 bytes by default. Passing `-1` gets the server's maximum, about 1 MiB. On the session endpoints the parameters are `tool_use_input_max_bytes` and `tool_result_max_bytes` [C2]. On chat messages they are `tool_use_input_max_chars` and `tool_result_max_chars`, and `-1` removes the limit [C8].
  - The importer always passes `-1` (ours), so a ledger is as complete as the API allows.
  - A block still cut carries `truncated: true`, and a truncated `input` is no longer valid JSON [C2]. The importer keeps it as a string and records `truncated`.

**Errors** [C5]:

| Response | What the importer does |
|---|---|
| 429 | Waits the `retry-after` seconds, or backs off exponentially from 1 s to 60 s when the header is missing. It never advances a cursor past a failed page. |
| 500 with `x-should-retry: false` | Does not retry. |
| Any other 500, or 502, 503, 504, 529 | Backs off exponentially. |
| A local-session 503 that says "Try again later." | Skips that session and retries it on the next run, with a new walk. |
| A local-session 503 for a customer-managed key that cannot be used | Stops that organization's transcripts for this run. |

The shared limit is 600 requests per minute per parent organization, across every key. The remote session endpoints have a second budget on top [C1][C5]. The importer reads `anthropic-ratelimit-requests-remaining` and slows before it runs out (ours), so it does not starve the org's other compliance tools.

## Writing: one ledger per import of a conversation

**When a conversation is imported (ours).** Sessions and chats keep growing, but a signed ledger is closed. So:

- The importer waits until a conversation has been quiet for `settle` (default 1 hour; ours). Quiet means `updated_at` for sessions and chats, and a final status for remote sessions.
- It then writes one ledger of the messages it has not recorded before.
- If the conversation gains messages later, the next import writes a new ledger with only those messages. That ledger is a *segment*: its `run.start` names the previous segment and carries that segment's attestation digest, so the segments of one conversation form a chain.

**Run id (ours):** `<source>-<conversation id>-<segment>`, for example `cowork-clls_01Hx...-1`. `<source>` is the `product_surface` for sessions, and `chat` for chats. It is deterministic, which gives two properties:
- `Recorder.start` refuses to reuse an existing path, so a segment is never written twice.
- A run killed between signing and saving its state loses nothing. The next run finds the segment already written, reads its last message id, and carries on from there.

**Which messages are new.** Message ids are stable while a turn is kept [C2]. The importer records each message id once per conversation and appends only ids it has not seen, in the order the API returns them. The API asks callers to keep that order and not re-sort by timestamp [C2].

- A message that was recorded and later changed upstream (compaction markers, retention) is not rewritten. The ledger keeps what the API returned when it was imported.
- A chat that comes back with `deleted_at` set gets a segment with no messages. Its `run.start` records `compliance.deleted_at`, so the ledgers show the chat was deleted after it was archived.

**`run.start` metadata:**

| Attribute | From |
|---|---|
| `principal.id` | the owner's `user.id`, which never changes and survives account deletion [C2][C3] |
| `principal.name` | the owner's email, from the list endpoint. The messages endpoints return it as `null` [C2]. |
| `principal.auth` | `"compliance_api"` (ours) |
| `run.name` | the chat's `name` for chats; none for sessions, since sessions carry no name [C2] |
| `compliance.source` | `local_session`, `remote_session` or `chat` |
| `compliance.id` | the session or chat id |
| `compliance.organization_uuid`, `compliance.product_surface`, `compliance.workspace_id`, `compliance.project_id` | as returned |
| `compliance.agent_id` | set on agent-owned remote sessions, e.g. Cowork scheduled tasks [C2] |
| `compliance.started_by` | the user who started an agent-owned remote session [C2] |
| `compliance.created_at`, `compliance.updated_at`, `compliance.status`, `compliance.deleted_at` | as returned |
| `compliance.segment`, `compliance.previous` | the segment number, and the previous segment's run id and attestation digest |
| `compliance.request_ids` | the `request-id` of each response the ledger was built from. Anthropic says to log these for chain of custody [C6]. |

**Events (ours).** The mapping reuses the kinds the gateway writes, so `reconstruct`, `report` and `verify` work unchanged. Every event carries:
- `compliance.message_id`, the API's message id;
- `compliance.created_at`, the API's timestamp for the message;
- `compliance.provenance` when it is set.

An event's own `ts` is when seatbelt imported it, not when the message happened.

| Transcript block | Event |
|---|---|
| user `text` | `user.message`, the actor being the owner |
| assistant `text` | `model.response`, with `gen_ai.response.model` set to the message's `model` (local sessions) or the chat's `model` (chats), else null. Chat text blocks also carry `thinking_redacted` [C8]. |
| `tool_use` | `tool.call`, parented to that message's `model.response` and keyed by the block's `id`. The arguments are parsed from `input` when it is whole. When it is truncated or not JSON, they are `{}` and the raw string goes in `compliance.input`. `truncated`, `integration_name` and `mcp_server_url` are kept when present [C8]. |
| `tool_result` | `tool.result`, parented to the call with the same `tool_use_id`, with the text entries as the result. `is_error: true` records the text as the error. |
| `content_unavailable` (local), or `content_unavailable: true` (remote) | a `user.message` or `model.response` with empty content and `compliance.provenance` naming the reason, such as `not_captured`, `client_aborted`, `retention_elapsed` or `oversize` [C2] |
| `synthetic_marker`, `client_asserted` | recorded like other content, with their `compliance.provenance`. The report can then tell a claimed turn from a verified one [C2]. |
| files, generated files, artifacts on a chat message | `compliance.files`, `compliance.generated_files` and `compliance.artifacts` on that message's event, as metadata only [C3] |

No `model.request` events are written. The API returns the conversation, not the requests that produced it, and making requests up would record something that was never seen.

**Redaction.** Anthropic does not mask credentials or personal data in transcripts [C2]. Every event goes through `Recorder`, which runs seatbelt's redaction as it does for the gateway.

**Unknown values.** Anthropic asks callers to pass through `product_surface`, `status` and `provenance` values they do not know, and to ignore fields they do not expect [C2]. The importer records them as returned, and treats any block type it does not know as text holding the block's JSON.

**Signing and storage.** The importer signs each segment as the gateway does, then ships it to the sink when one is configured. It uses the gateway's config file: `signing_key`, `ledgers` and `sink` [repo: `src/seatbelt/gateway/config.py`], plus a new `compliance:` block. One public key then verifies both kinds of ledger.

## Configuration and running

```yaml
compliance:
  key_env: ANTHROPIC_COMPLIANCE_ACCESS_KEY   # env var name; never the key
  sources: [local_sessions, remote_sessions, chats]
  surfaces: null          # e.g. [cowork, cowork_remote, office_agents/excel]; null: every surface
  since: 2026-09-01T00:00:00Z   # first run only: how far back to start
  settle: 3600            # seconds a conversation must be quiet before it is imported
  overlap: 900            # seconds the local-session window reaches back before the last run
```

- **`surfaces`** is how an org that routes Claude Code through the gateway avoids recording it twice. With `[cowork, cowork_remote]`, for instance, the gateway keeps Claude Code and the importer covers Cowork.
- **`seatbelt import compliance --config gateway.yaml`** runs once and exits. It is run on a schedule: cron, a systemd timer, or a Coolify scheduled task (ours).
- **State** is kept in `ledgers/.compliance/state.json`, written atomically. It holds each source's watermark or cursor, and for each conversation the last message id recorded and the last segment.
- **Recovery:** because the run ids are deterministic, a lost state file costs no evidence. The importer rebuilds the state from the ledgers. Rebuilding does mean re-reading the API from `since`.
- **At the end of each run** it logs, per source:
  - the cursors it started and ended with;
  - how many conversations and messages it imported;
  - the `request-id` of the last page.

  Anthropic suggests exactly this to show an export is complete, since the lists have no counts or checksums [C6].

## Retention: why this is worth having

Anthropic's copy does not always outlast what an org must keep [C2][C6]:
- **Local sessions** are kept 6 years by default, or for the org's custom retention period when a finite one is set, even a shorter one.
- **Remote sessions** are kept 6 years, unless the user deletes the session.
- **Chat content** follows the org's retention policy, and is gone when a user deletes the chat.

For any horizon longer than these, or a legal hold that must survive user deletion, Anthropic's advice is to export as you ingest [C6]. An imported, signed ledger is that export, and it is tamper-evident. Keeping it for the full horizon is the org's storage choice; the optional sink is one way.

## Testing (ours)

- A fake Compliance API (Starlette, like the fake providers in the gateway tests). It serves the documented shapes: paging, short pages, `pending` sessions, `deleted_at`, the provenance cases, truncation, and 429, 500 and 503 with their headers.
- Unit tests of the event mapping from the documented example responses [C2][C3].
- End-to-end runs:
  - an import, new messages, then a second import, checking that the segments chain;
  - a run killed after signing, then recovery;
  - a lost state file;
  - `verify`, `reconstruct` and `report` over the imported ledgers.
- There is no Enterprise tenant to test on (Decided, 5). The importer is tested against the fake only, and its docs say so.

## Decided (2026-09-29)

1. **Who a person is: later.** Imported ledgers name the owner by Anthropic's `user.id`. Gateway ledgers name them by issued-key principal or OIDC subject, so `seatbelt report` shows the same person as two principals. A mapping from Anthropic user id to principal is a later change.
2. **`settle`: 1 hour.** A long-running session that goes quiet for an hour, then resumes, becomes two segments.
3. **File and artifact content: metadata only.** The first version records ids, names, sizes and MD5s, and downloads nothing.
4. **Deletion: documented, no tooling yet.** A signed ledger keeps content that a user later deleted in claude.ai. That is the point for a legal hold, but it may conflict with an org's data-protection duties, and the user docs say so plainly. There is no tool yet for dropping a person's ledgers.
5. **The real API: not tested.** There is no Enterprise tenant to test on. The importer is built and tested against a fake of the documented API. Its docs and the changelog say it has not been run against a live tenant.

## Source map

Read on 2026-09-29, as each page's Markdown (`<url>.md`).

| Ref | Source | Used for |
|---|---|---|
| C1 | platform.claude.com/docs/en/manage-claude/compliance-api | base URL, `x-api-key`, `anthropic-version`; Enterprise-only content endpoints; Console orgs get the Activity Feed only; the 600/min shared limit and the second budget for remote sessions |
| C2 | platform.claude.com/docs/en/manage-claude/compliance-sessions | local and remote session endpoints, filters, ordering, paging, 24-hour cursors; the `updated_at.gte` overlap advice; `status` values; agent-owned sessions; the transcript shape, `provenance` types and reasons, placeholders, stable message ids, "preserve the returned order"; truncation parameters and `-1`; email null on the messages endpoints; no masking of credentials or personal data; forward-compatible handling; what is not captured; retention periods |
| C3 | platform.claude.com/docs/en/manage-claude/compliance-content-data | chat list `order_by=updated_at` with `after_id`, what makes a chat reappear, `deleted_at`; chat messages shape with `files`, `generated_files`, `artifacts`; stored files may differ from uploads; Admin keys get 403 |
| C4 | platform.claude.com/docs/en/manage-claude/compliance-api-access | key types and prefixes, who creates them, scopes, least-scope advice, the `ANTHROPIC_COMPLIANCE_ACCESS_KEY` example name, the 403 message listing scopes |
| C5 | platform.claude.com/docs/en/manage-claude/compliance-errors | 429 with `retry-after` and the rate-limit headers, backoff 1–60 s, do not advance the cursor; `x-should-retry` on 500; 502/503/504/529 transient; the local-session 503 cases |
| C6 | platform.claude.com/docs/en/manage-claude/compliance-integration-patterns | retention table and export advice; at-least-once delivery; what to log to show an export is complete; request ids and provenance for chain of custody; what the API does not include |
| C7 | platform.claude.com/docs/en/manage-claude/compliance-faq | the Activity Feed has no message content; what session transcripts include, local and remote |
| C8 | platform.claude.com/docs/en/api/compliance (Get chat messages) | chat messages parameters (`tool_*_max_chars`, `-1`, `limit` omitted returns everything); `text.thinking_redacted`; `tool_use` and `tool_result` fields including `integration_name` and `mcp_server_url` |
| repo | `src/seatbelt/record/recorder.py`, `ledger/store.py`, `gateway/config.py`, `gateway/app.py`, `report/fleet.py`, `report/timeline.py` at `ec1d604` | run id pattern and refusal to reuse a path; `ts` set at append; config fields; the `principal.*` attributes; which kinds and attributes the report and timeline read |
