# Compliance API importer (0.3.0): design

**Goal:** `seatbelt import compliance` pulls the transcripts Anthropic keeps for a Claude Enterprise organization into signed seatbelt ledgers. It covers what the gateway cannot see:
- claude.ai chats;
- Cowork, on the desktop and in the cloud;
- Claude Code, Claude for Microsoft 365, Claude Science and Claude in Chrome sessions on machines whose traffic does not go through a gateway.

**What a ledger from the importer attests.** A gateway ledger says "these bytes crossed the gateway". An imported ledger says "this is what Anthropic's Compliance API returned when seatbelt asked, at this time".
- The transcript is Anthropic's reconstruction, with the gaps Anthropic documents (below).
- So every imported ledger names its source, and every event carries the API's own ids and timestamps.
- The seatbelt signature proves only that the ledger has not changed since the import.

**Status:** implemented in `seatbelt.compliance`, and tested against a fake of the documented API only; the user guide is `docs/deploy/compliance-import.md`. The maintainer's decisions are under "Decided".
- Every Compliance API fact comes from the sources in the source map at the end, read on 2026-09-29.
- Decisions that are ours, not the API's, are marked "(ours)".
- Where the design relies on something the sources leave open, it says so.

## Scope

**Who can use it.** Transcript content (chats and sessions) is served to Claude Enterprise organizations only [C1][C6]. A standalone Claude Console organization can read the Activity Feed only [C1], and the Activity Feed carries no prompt or response text [C7]. The roadmap said "Team and Enterprise"; this change corrects it to Enterprise.

**Imported, first version (ours):**

| Source | Endpoints | Covers |
|---|---|---|
| Local sessions | `GET /v1/compliance/apps/sessions/local`, `/{id}`, `/{id}/messages` [C2] | Cowork in Claude Desktop, Claude Code (terminal, desktop, IDE), Claude Science, Claude for Microsoft 365 and Claude in Chrome, on the user's machine [C2] |
| Remote sessions | `GET /v1/compliance/apps/sessions/remote`, `/{id}/messages` [C2] | Cowork started on claude.ai web or mobile, run in the cloud [C2] |
| Chats | `GET /v1/compliance/apps/chats`, `/{id}/messages` [C3] | claude.ai chats |

**Not imported, first version (ours):**
- **The content of files, generated files and artifacts.** Their metadata on each chat message is recorded [C3][C8]:
  - files and generated files: `id`, `filename`, `mime_type`, `size_bytes` and `md5`, any of which but the id can be null;
  - artifacts: `id`, `version_id`, `title` and `artifact_type`, with no size or hash.
- **Projects and project attachments.**
- **The Activity Feed.** It has no message content [C7].
- **Deletes.** The importer never calls a `DELETE` endpoint.

**Never in the API, so never in an imported ledger** [C2][C6][C7]:
- thinking blocks;
- the system prompts of local sessions (a marker stands in);
- tool definitions and MCP configuration;
- images and other binary content. In local sessions a placeholder stands in; in remote sessions and chat tool results the content is left out;
- token usage and cost;
- Claude Code with a Console API key, run through Amazon Bedrock, Google Cloud or Microsoft Foundry, or run in Claude Code cloud sessions;
- local sessions in HIPAA-ready organizations or under zero data retention;
- content already removed by retention or deleted by a user.

## Key and access

- **Key:** a Compliance Access Key (`sk-ant-api01-...`), made in claude.ai > Organization settings > API [C4].
  - The primary owner can make one for the whole parent organization; an organization owner, for their own organization [C4].
  - Admin API keys get 403 on the content and session endpoints [C2][C3].
- **Scope:** `read:compliance_user_data` covers chats, messages, session metadata and transcripts [C4].
  - The docs advise giving an integration that never deletes no `delete:compliance_user_data` [C4], and the importer's docs say the same.
  - The importer cannot check a key's scopes in advance. A call the key's scopes don't cover returns 403, listing the scopes the key has [C4].
- **Where the key is read:** an environment variable, never the config file (ours, as for the sink's credentials).
  - The default is `ANTHROPIC_COMPLIANCE_ACCESS_KEY`, the name the guides use [C4].
  - The key is never logged or recorded.
- **Every request** goes to `https://api.anthropic.com/v1/compliance/...` with `x-api-key` and `anthropic-version: 2023-06-01` [C1].

## Pulling: what changed since the last run

The API gives each source different filters and cursors, so each is walked differently. As well as the cursors, the importer keeps a **pending list** in its state (ours): every conversation it has seen that has not settled yet (see `settle`, below). Each run fetches the pending conversations directly, so a conversation that goes quiet is imported even after the list filters stop returning it.

**Local sessions** [C2]:
- **Filters and paging.** The list has no user filter. It takes `created_at.gte`, `created_at.lt` and `updated_at.gte`. It pages forward with `page` and `next_page`, newest first, `limit` up to 500, until `next_page` is `null`.
- **Finding sessions active since the last run:** pass `updated_at.gte`, set "a few minutes before your previous run's start time". A session's last call can still be indexing, and a bound set exactly at the previous run can drop a session for good.
  - The importer uses the previous run's start minus `overlap`, default 15 minutes (ours).
  - Pending sessions are re-read with `GET .../local/{id}`.
- **Timing.** A list walk should finish within 24 hours of its first page.

**Remote sessions** [C2]:
- **Filters and paging.** The list has no `updated_at` filter, only `created_at` bounds. It pages with `page` and `next_page`, newest first.
- **Status.** Each session has a `status`: `pending`, `active`, `paused`, `archived` or `failed`.
  - A `pending` session has no transcript yet, and its messages return 404.
  - Deleted sessions are never returned, and their messages return 404.
- **Which sessions are final (ours).** The sources do not say which statuses are final, and a paused session can resume. The importer treats a session as final when it is:
  - `archived` or `failed`;
  - gone from the list;
  - answering 404 to its messages while not `pending`, which means it was deleted.
- **Re-listing (ours).** The importer re-lists by `created_at` back to the oldest session it still follows. It follows a session that is not final and has had activity in the last 30 days. There is no single-session retrieve endpoint for remote sessions. A session left quiet for 30 days and then resumed is picked up only if it is in a later listing.
- **Where fields come from.** The messages response always returns `user.email_address`, `started_by_user` and `claude_project_id` as `null` [C2]. The importer takes them from the list.

**Chats** [C3]:
- **Filters and paging.** The organization-wide list with `order_by=updated_at` pages oldest first, passing the previous page's `last_id` as `after_id`, until `has_more` is false.
- **Keeping the export current.** Saving the final `last_id` and resuming from it is Anthropic's recommended way to keep an export current [C3].
  - A chat comes back after the cursor when it gets a new message, is moved into or out of a project, or is deleted in claude.ai.
  - A rename may not bring it back.
  - A chat that comes back with `deleted_at` set has no content left.

**Transcripts:**
- **Local sessions** [C2]: oldest first, paged with `page` and `next_page`, `limit` up to 1,000.
  - A page can be short without being the last, so the walk goes on until `next_page` is `null`.
  - Messages cursors expire 24 hours after a walk's first page.
- **Remote sessions** [C2]: the same paging, including short pages. The sources give no expiry for these cursors, so the importer finishes each walk in one run and never saves a messages cursor (ours).
- **Chats** [C8]: the whole chat comes in one response when `limit` is left out, and the importer leaves it out.

**Truncation:**
- **Defaults.** Each tool input, and each text entry of a tool result, is cut at 10,000 by default: bytes on the session endpoints [C2], characters on chat messages [C8].
- **Raising the limit.** The parameters are:
  - sessions: `tool_use_input_max_bytes` and `tool_result_max_bytes`, where `-1` gives the server maximum, about 1 MiB [C2];
  - chats: `tool_use_input_max_chars` and `tool_result_max_chars`, where `-1` disables the limit, "subject to any server-side maximum the endpoint enforces" [C8].
- **What the importer does (ours).** It always passes `-1`, so a ledger is as complete as the API allows.
- **What still gets cut.** A block that is still cut carries `truncated: true`, and a truncated `input` is no longer valid JSON [C2]. The importer keeps it as a string and records `truncated`.

**Errors** [C5]:

| Response | What the importer does |
|---|---|
| 429 | Waits the `retry-after` seconds. With no header, it backs off exponentially from 1 s to 60 s. It never advances a cursor past a failed page. |
| 429 from the remote sessions' own budget | `retry-after` is always `1`, and the `anthropic-ratelimit-*` headers describe the shared limit, not this budget. So when the 429 repeats, the importer backs off exponentially. |
| 500 with `x-should-retry: false` | Does not retry. |
| Any other 500, or 502, 503, 504, 529 | Backs off exponentially. |
| Local 503 "Try again later." on retrieve or messages | Skips that session, which stays pending, and retries it on the next run with a new walk. |
| Local 503 "Try again later." on the list | Stops the list walk. The next run restarts it without `page`, from the same bound. |
| Local 503 "Captured content ... Try again shortly." | Backs off. If it keeps recurring for one organization, it stops that organization's transcripts for this run. The sources say recurrence is the only sign that a customer-managed key is the cause. |

**Rate limit.** The shared limit is 600 requests per minute per parent organization, across every key. The remote session endpoints have a second budget on top [C1][C5]. The importer reads `anthropic-ratelimit-requests-remaining` and slows down before the shared limit runs out (ours), so it does not starve the org's other compliance tools. That header does not track the remote budget [C5].

## Writing: one ledger per import of a conversation

**Where (ours).** The importer writes to its own folder, by default `compliance/` inside the gateway's ledgers folder, never into the gateway's folder itself. The gateway reads only the top level of its folder, so a subfolder is safe. A lock file, `.lock`, stops two imports from running on one folder at once, since each would close the other's open segment.
- **Why:** at start-up the gateway closes and signs every open ledger in its folder. That would include a segment the importer is still writing [repo: `gateway/sessions.py`, `gateway/serve.py`].
- **Consequence:** the report and the sink read one folder, not its subfolders [repo: `report/fleet.py`, `gateway/sink.py`]. So:
  - `seatbelt report` takes the importer's folder as its own argument;
  - the importer ships its own ledgers when a sink is configured.

**When a conversation is imported (ours).** Sessions and chats keep growing, but a signed ledger is closed. So:
- **Waiting to settle.** The importer waits until a conversation has been quiet for `settle`, 1 hour. Quiet means:
  - `updated_at` at least an hour old, for every source;
  - for remote sessions, also final, as defined above, however recent.
- **Writing.** It then writes one ledger of the messages it has not recorded before.
- **Later messages.** If the conversation gains messages, the next import writes a new ledger with only those. That ledger is a *segment*: its `run.start` names the previous segment and carries that segment's `ledger_sha256`, from its attestation, so one conversation's segments form a chain.

**Run id (ours).** `<source>-<conversation id>-<segment>`, for example `cowork-clls_01Hx...-1`.
- `<source>` is the `product_surface` for sessions, with every character outside `[A-Za-z0-9_.-]` made `.`, so `office_agents/excel` becomes `office_agents.excel`. A null surface becomes `unknown` [C2][C8][repo: `record/recorder.py`].
- `<source>` is `chat` for chats.
- The run id is deterministic, and `Recorder.start` refuses to reuse an existing path. So a segment is never written twice.

**A killed run (ours).**
- **Killed after signing, before saving its state.** The next run finds the segment already written, reads its last message id, and carries on.
- **Killed before signing.** The segment is left open. The next run:
  1. closes it with a failed `run.end` ("importer killed");
  2. signs it, as the gateway does for its own open ledgers;
  3. carries on from its last message id.

  The messages in it stay recorded once.
- **A lost state file** costs no evidence: the importer rebuilds it from the ledgers, re-reading the API from `since`.

**Which messages are new.** For local sessions, a message id is stable while its turn is kept [C2][C8]. The sources say nothing on this for remote sessions or chats, and the design assumes it.
- The importer appends, in the order the API returns them, only the messages after the last message it recorded. The API asks callers to keep that order and not re-sort by timestamp [C2].
- **If that last message is no longer in the transcript because it aged out,** everything still kept is new. Retention removes the oldest turns first, and a transcript whose start has aged out begins with a `retention_elapsed` placeholder [C2]. The importer records the messages after the placeholder, but not the placeholder, which stands for turns already recorded. The placeholder gets a new id each time more turns age out [C8], so it is recorded only when it opens a conversation's first segment.
- **If that last message is gone for any other reason,** the importer cannot tell what is new. It writes nothing for that conversation and logs it for a person to check. Guessing could record a message twice or drop one.
- **A message that was recorded and later changed upstream** (compaction, retention) is not rewritten. The ledger keeps what the API returned when it was imported.
- **A chat that comes back with `deleted_at` set** gets a segment with no messages. Its `run.start` records `compliance.deleted_at`, so the ledgers show the chat was deleted after it was archived.

**`run.start` metadata:**

| Attribute | From |
|---|---|
| `principal.id` | the owner's `user.id`, which stays the same when the email or name changes [C6]. For local sessions it survives account deletion [C2]. For agent-owned remote sessions, which have no `user`, it is `started_by_user.id`. For a chat with no `user` (a key limited to one organization, whose creator has left it [C8]), it is `unknown`. |
| `principal.name` | the owner's email, from the list endpoint. The session messages endpoints return it as `null` [C2]. |
| `principal.auth` | `"compliance_api"` (ours) |
| `run.name` | the chat's `name` for chats. None for sessions, which carry no name [C2]. |
| `compliance.source` | `local_session`, `remote_session` or `chat` |
| `compliance.id` | the session or chat id |
| `compliance.organization_uuid`, `compliance.product_surface`, `compliance.workspace_id`, `compliance.project_id`, `compliance.created_at`, `compliance.updated_at`, `compliance.status`, `compliance.deleted_at` | from the list or retrieve response (for remote sessions, the list) |
| `compliance.agent_id` | the agent, on agent-owned remote sessions such as Cowork scheduled tasks [C2] |
| `compliance.started_by` | the user who started an agent-owned remote session [C2] |
| `compliance.segment`, `compliance.previous` | the segment number, and the previous segment's run id and `ledger_sha256` (null when unsigned) |
| `compliance.endpoint`, `compliance.query`, `compliance.request_ids` | the endpoint and query parameters the ledger was built from, and each response's `request-id` (ours). For chain of custody, Anthropic advises storing each record with its source endpoint, query parameters, run timestamp and a content hash [C6]. The run timestamp is the events' `ts`, and the hash is each event's own. |

**Events (ours).** The mapping uses the same event kinds the gateway writes, so `reconstruct` and `verify` work unchanged.
- **Every event carries:**
  - `compliance.message_id`, the API's message id;
  - `compliance.created_at`, the API's timestamp for the message;
  - `compliance.provenance`, when set.
- **An event's own `ts`** is when seatbelt imported it, not when the message happened [repo: `ledger/events.py`].

| Transcript block | Event |
|---|---|
| user `text` | `user.message`, with the owner as the actor. On an agent-owned remote session, the actor is the message's `sent_by_user_id` when set, and otherwise `started_by_user` [C2]. |
| assistant `text` | `model.response`. `gen_ai.response.model` is the message's `model` for local sessions [C2], and null for remote sessions, whose messages carry none. For chats, the chat's `model` goes in `compliance.chat_model` instead: it is the model selected for the chat, not necessarily the one that served each message, and it is null for legacy chats [C8]. Chat text blocks also carry `thinking_redacted` [C8]. |
| `tool_use` | `tool.call`, parented to that message's `model.response` and keyed by the block's `id`. When the `id` is null [C8], the key is `<message id>#<block index>`. The arguments are parsed from `input` when it is whole; when it is truncated or not JSON, they are `{}` and the raw string goes in `compliance.input`. `truncated`, `integration_name` and `mcp_server_url` are kept when present [C8]. |
| `tool_result` | `tool.result`, with the text entries as the result, and `is_error: true` recorded as the error. It is parented to the call whose key is its `tool_use_id`. When `tool_use_id` is null [C8], it is parented to the oldest unanswered call with the same `name`; when there is none, it has no parent. |
| local `content_unavailable` | a `user.message` or `model.response` with empty content. `compliance.provenance` names the reason: `not_captured`, `client_aborted`, `retention_elapsed`, `oversize`, or any other [C2]. |
| remote `content_unavailable: true` | the same, with `compliance.provenance` set to `{"type": "content_unavailable"}`. Remote messages carry no reason [C2][C8]. |
| local `synthetic_marker`, `client_asserted` | recorded like other content, with their `compliance.provenance` [C2] |
| files, generated files, artifacts on a chat message | `compliance.files`, `compliance.generated_files` and `compliance.artifacts` on that message's event, as metadata only [C3][C8] |

**No `model.request` events.** The API returns the conversation, not the requests that produced it. Making requests up would record something that was never seen.

**What this needs in the code (ours):**
- **`Recorder`** can write a `model.response` only after a `model.request`, and cannot put extra attributes on user messages or tool results [repo: `record/recorder.py`]. It gains:
  - `model_responded(model, content, attrs)`, with no request and a model that may be null;
  - an `attrs` argument on `user_message` and `tool_returned`.

  Every event still goes through `Recorder`, so seatbelt's redaction runs on it. That matters: Anthropic does not mask URLs, credentials or personal data in local session transcripts [C2], and the sources don't say remote sessions or chats are masked either.
- **`seatbelt report`** today counts every `model.response` as a model call, and would group a null model as `None` [repo: `report/fleet.py`]. It changes to:
  - leave out imported responses whose `compliance.provenance` is set (markers, client-asserted and unavailable turns);
  - group a null model as `unknown`.
- **The timeline** gains no new rendering in the first version.
- **`GatewayConfig`** rejects unknown keys and requires `upstreams` [repo: `gateway/config.py`]. It gains an optional `compliance:` block, and `upstreams` becomes optional when that block is present.

**Unknown values.** Anthropic asks callers to pass through `product_surface` and `status` values they don't know, and to ignore fields they don't expect. They are also to tolerate `provenance` types and reasons they don't know [C2]. The importer records all of them as returned, and treats a block type it doesn't know as text holding the block's JSON.

**Signing.** The importer signs each segment with the gateway's signing key, so one public key verifies both kinds of ledger. It reads `signing_key` and the optional `sink` from the gateway's config [repo: `gateway/config.py`].

## Configuration and running

```yaml
compliance:
  key_env: ANTHROPIC_COMPLIANCE_ACCESS_KEY   # env var name; never the key
  ledgers: /var/lib/seatbelt/compliance      # default: compliance/ beside the gateway's ledgers
  sources: [local_sessions, remote_sessions, chats]
  surfaces: null          # e.g. [cowork, cowork_remote, office_agents/excel]; null: every surface
  since: 2026-09-01T00:00:00Z   # first run only: how far back to start
  settle: 3600            # seconds a conversation must be quiet before it is imported
  overlap: 900            # seconds the local-session window reaches back before the last run
```

- **`surfaces`** stops an org that routes Claude Code through the gateway from recording it twice. With `[cowork, cowork_remote]`, the gateway keeps Claude Code and the importer covers Cowork.
- **`seatbelt import compliance --config gateway.yaml`** runs once and exits. It is run on a schedule, such as cron, a systemd timer or a Coolify scheduled task (ours).
- **State** lives in `<compliance ledgers>/.state.json`, written atomically. It holds:
  - each source's watermark or cursor;
  - the pending list;
  - for each conversation, the last message id recorded and the last segment.
- **Each run logs, per source:**
  - the start time;
  - the cursors it started and ended with;
  - how many conversations and messages it imported;
  - the `request-id` of the last page.

  The lists have no counts or checksums, and this is what Anthropic suggests logging to show an export is complete [C6].

## Retention: why this is worth having

Anthropic's copy does not always outlast what an org must keep [C2][C6]:
- **Local sessions** are kept for 6 years by default, or for the org's custom retention period when a finite one is set, even a shorter one.
- **Remote sessions** are kept for 6 years, unless the user deletes the session.
- **Chat content** follows the org's retention policy, and is gone when a user deletes the chat.

For a longer horizon, or a legal hold that must survive user deletion, Anthropic's advice is to export as you ingest [C6]. An imported, signed ledger is that export, and it is tamper-evident. Keeping it for the full horizon is the org's storage choice.

## Testing (ours)

- **A fake Compliance API** in Starlette, like the fake providers in the gateway tests. It serves the documented shapes:
  - paging, including short pages;
  - `pending` sessions and deleted sessions;
  - `deleted_at`;
  - the provenance cases and truncation;
  - null tool ids and a null `product_surface`;
  - 429, 500 and 503 with their headers.
- **Unit tests** of the event mapping, using the documented example responses [C2][C3].
- **End-to-end runs:**
  - an import, new messages, then a second import, checking that the segments chain;
  - a run killed before signing, and one killed after signing;
  - a lost state file;
  - a conversation whose last recorded message has gone;
  - `verify`, `reconstruct` and `report` over the imported ledgers.
- **Not tested against the real API.** There is no Enterprise tenant to test on (Decided, 5). The importer is tested against the fake only, and its docs say so.

## Decided (2026-09-29)

1. **Who a person is: later.** Imported ledgers name the owner by Anthropic's `user.id`. Gateway ledgers name them by issued-key principal or OIDC subject, so `seatbelt report` shows the same person as two principals. A mapping from Anthropic user id to principal is a later change.
2. **`settle`: 1 hour.** A long-running session that goes quiet for an hour, then resumes, becomes two segments.
3. **File and artifact content: metadata only.** The first version records the metadata listed under Scope, and downloads nothing.
4. **Deletion: documented, no tooling yet.** A signed ledger keeps content that a user later deleted in claude.ai. That is the point for a legal hold, but it may conflict with an org's data-protection duties, and the user docs say so plainly. There is no tool yet for dropping a person's ledgers.
5. **The real API: not tested.** There is no Enterprise tenant to test on. The importer is built and tested against a fake of the documented API. Its docs and the changelog say it has not been run against a live tenant.

## Source map

Read on 2026-09-29, as each page's Markdown (`<url>.md`). Checked claim by claim by a second reader against the same files.

| Ref | Source | Used for |
|---|---|---|
| C1 | platform.claude.com/docs/en/manage-claude/compliance-api | base URL, `x-api-key`, `anthropic-version`; content endpoints for Enterprise only; Console orgs get the Activity Feed only; the 600/min shared limit and the remote sessions' second budget |
| C2 | platform.claude.com/docs/en/manage-claude/compliance-sessions | local and remote session endpoints, filters, ordering, paging and short pages; 24-hour local cursors; the `updated_at.gte` overlap advice; remote `status` values, deleted sessions, fields null on the remote messages envelope; agent-owned sessions; transcript shape, `provenance` types and reasons, placeholders; local message ids stable while retained; "preserve the returned order"; truncation parameters and `-1`; email null on the session messages endpoints; `user.id` survives account deletion (local); no masking in local transcripts; handling unknown values; what is not captured; retention |
| C3 | platform.claude.com/docs/en/manage-claude/compliance-content-data | chat list `order_by=updated_at` with `after_id`, what brings a chat back, `deleted_at`; chat messages with `files`, `generated_files`, `artifacts`; Admin keys get 403 |
| C4 | platform.claude.com/docs/en/manage-claude/compliance-api-access | key types and prefixes, who creates them, scopes, least-scope advice, the `ANTHROPIC_COMPLIANCE_ACCESS_KEY` name in the guides, the 403 that lists scopes |
| C5 | platform.claude.com/docs/en/manage-claude/compliance-errors | 429 with `retry-after` and the rate-limit headers, backoff from 1 s to 60 s, not advancing the cursor; the remote budget's 429; `x-should-retry` on 500; 502, 503, 504 and 529 are transient; the three local-session 503 bodies and how to handle each |
| C6 | platform.claude.com/docs/en/manage-claude/compliance-integration-patterns (and the Activity Feed page it links, for `user_id`) | retention table and export advice; what to log to show an export is complete; chain-of-custody metadata; what the API does not include; `user_id` stable across email and name changes |
| C7 | platform.claude.com/docs/en/manage-claude/compliance-faq | the Activity Feed has no message content; what local and remote transcripts include; non-text blocks left out of remote transcripts |
| C8 | platform.claude.com/docs/en/api/compliance (Get chat messages, Retrieve local and remote session messages) | chat messages parameters (`tool_*_max_chars`, `-1`, all messages when `limit` is left out); chat `model` is the selected model and can be null; chat `user` can be null; `text.thinking_redacted`; `tool_use` and `tool_result` fields, ids that can be null; file and artifact fields; the local `retention_elapsed` placeholder id; remote `content_unavailable`; `product_surface` can be null |
| repo | at `ec1d604`: `record/recorder.py`, `ledger/store.py`, `ledger/events.py`, `gateway/config.py`, `gateway/app.py`, `gateway/sessions.py`, `gateway/serve.py`, `gateway/sink.py`, `report/fleet.py`, `report/timeline.py` | run id pattern and refusal to reuse a path; `Recorder` methods; `ts` set on append; config fields and strictness; `principal.*` attributes (`sessions.py`, `app.py`); the gateway closing open ledgers at start; single-folder reads in the report and the sink; how the report and timeline read events |
