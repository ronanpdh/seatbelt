# 6. Recording gateway: one org service records every client

- Status: accepted
- Date: 2026-09-28

## Context

Recording needed the agent's code to call the `Recorder` or wrap an SDK client. Employees run Claude Code, Claude Desktop and Cowork, and SDK agents they did not write, so almost nothing was recorded, what was recorded sat on the employee's own disk, and nothing bound a run to a person.

## Decision

`seatbelt gateway serve` is an HTTP service the organisation runs. Clients point their base URL at it. It speaks provider wire formats, not models: Anthropic Messages (`/v1/messages`) and OpenAI Chat Completions (`/v1/chat/completions`), one module per format under `seatbelt.gateway.formats`, shared with the Anthropic SDK adapter. It holds the real provider keys, forwards each request's body and query unchanged, relays the response (decoded, streamed chunk by chunk or not), and records the exchange through the existing `Recorder`. Ledgers, sidecars and the signing key stay on the gateway host.

- **Identity by issued key.** `seatbelt gateway keygen --user <id>` prints a random key once and stores its SHA-256 in the config. An unknown key is 401 and nothing is written. `run.start` carries `principal.id` and `principal.key_id`, set by the gateway after any client-supplied metadata so a client cannot overwrite them.
- **One ledger per session.** Requests with one key share a ledger until `session_idle` seconds of quiet; a client may name a run (`X-Seatbelt-Run`) and end it (`X-Seatbelt-Run-End`, or `POST /seatbelt/runs/<name>/end`). A session is never closed while a request holds it, so no event lands after `run.end`. On start the gateway closes and signs any chain a crash left open (`run.ok: false`), and skips and logs a ledger it cannot read or whose chain fails, rather than sign evidence of tampering or refuse to start.
- **Org policy at the one point a proxy has.** Allowed models and an output token cap refuse a request with 403 before it leaves. A denied tool the model asks for is recorded and relayed, since the gateway cannot stop a local tool; any later request carrying its result is refused, whether the session or the request's own history names it. Every verdict is a `policy.check`.
- **Failures are recorded, not fabricated.** Upstream errors are relayed and recorded; an unreachable upstream is 502 and recorded; a stream that breaks or that the client leaves is recorded as far as it got, with the reason, and without the tool calls whose inputs may be truncated.
- **Probes** Claude Code and Desktop make (`/api/hello`, `count_tokens`, `/v1/models`) are answered or forwarded, not recorded: a token count or a model list is nothing an auditor needs.

Rejected: a proxy on each employee's machine (the record sits on their disk; identity from the OS user is spoofable), SSO from day one (identity-provider integration in every client before anything records), one ledger per request (an auditor would reassemble a conversation from many files), Claude Code hooks as the capture path (tool-specific; the message history already carries every tool call and result).

## Consequences

- One service records coding and non-coding staff alike; a Cowork session is a session like any other.
- `verify`, `reconstruct`, `pack` and `verify-pack` work unchanged on gateway ledgers. `seatbelt report` aggregates them, counting only chains that verify.
- The gateway sees every prompt in clear before redaction; its host is as sensitive as the provider keys it holds.
- A shared key in a fleet-wide client profile attributes everyone to one principal; per-user attribution needs per-user keys.
- Traffic in the OpenAI Responses format (current Codex, the OpenAI Agents SDK's Responses model) or Gemini's is not recorded until those formats land (0.3.0).
- Config is read once at start: revoking a key means deleting its entry and restarting.
