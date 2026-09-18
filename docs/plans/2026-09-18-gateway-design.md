# Recording gateway and launcher design (0.2.0)

Date: 2026-09-18. Status: agreed, not yet implemented.

## Problem

Seatbelt's purpose is that an organisation can run its employees' agents through it and hold a tamper-evident, signed record of every prompt, model call, tool call and result. Today recording needs the agent's code to call the `Recorder` or wrap an SDK client. Employees run Claude Code, Codex, Cursor, Gemini CLI and SDK agents they did not write, so almost nothing gets recorded, the record sits on the employee's own disk, and nothing binds a run to a person.

## Decisions

- **A central recording gateway**, `seatbelt gateway serve --config gateway.yaml`, an org-run HTTP service that speaks the provider wire formats. Clients point their base URL at it. It holds the real provider keys, swaps them in, forwards, relays the response byte for byte (streamed or not), and records the exchange through the existing `Recorder`. Ledgers, sidecars and the signing key live on the gateway host; employees never touch them. `verify`, `reconstruct`, `pack` and `verify-pack` work unchanged on gateway output.
- **Wire formats, not models.** Four cover the market: Anthropic Messages (Claude Code, Claude Desktop and Cowork, Claude SDKs, Bedrock and Vertex via SDK base URL), OpenAI Chat Completions (Codex, Cursor, LangChain, Ollama, Mistral, Groq, Azure), OpenAI Responses (Agents SDK), Gemini `generateContent` (Gemini CLI). One translation module per format under `gateway/formats/` whose only job is: given request and response JSON or SSE, emit recorder events. The Anthropic logic already exists in the adapter and is lifted out. Unknown fields pass through untouched and are recorded raw, so a new model feature never blocks a call. Routing is by path prefix, so one URL serves every client; config maps model names to upstreams. Besides the inference endpoints, clients probe: Claude Code calls `POST /v1/messages/count_tokens` (forwarded, not recorded: a token count is nothing an auditor needs), `GET /v1/models` when discovery is enabled (forwarded, not recorded) and `HEAD /api/hello` (answered 200 locally). Per the Claude Code gateway compatibility guide (code.claude.com/docs/en/llm-gateway-protocol).
- **Non-coding staff through the same gateway.** Claude Desktop, which hosts Cowork, is pointed at an org gateway by MDM profile or in-app config (`inferenceProvider: gateway`, `inferenceGatewayBaseUrl`), with a static key, OIDC or a credential helper as auth; the gateway must serve `POST /v1/messages` with streaming and tool use, and pass `cache_control` breakpoints through untouched. Per claude.com/docs/third-party/claude-desktop/gateway. So one service records coders and non-coders alike; a Cowork session is a session like any other. The MDM profile template ships in `docs/deploy/`.
- **Identity by issued key.** `seatbelt gateway keygen --user alice@corp --config gateway.yaml` prints a random bearer key once and appends `{id, key_sha256, issued}` to the config. Unknown key is 401 and nothing is recorded. Revoke by deleting the line. The principal id is whatever the org uses (usually the email), so SSO later replaces the lookup, not the ledger. `run.start` carries `principal.id`, `principal.key_id`, `client.ip`, `client.user_agent`, gateway version.
- **Session per principal with idle timeout.** Requests from one key chain into one ledger until `session_idle` (default 15m) of quiet closes it: `run.end`, sign, immutable. A client may name a run with `X-Seatbelt-Run: <name>`; same principal and name continue that ledger until `X-Seatbelt-Run-End: true` or the idle window. Requests within a session are serialised by a per-session lock; sessions run in parallel. On startup the gateway closes any chain without a `run.end` (`run.ok=false`, `run.error="gateway restarted"`) and signs it, so a crash leaves no INCOMPLETE ledger behind.
- **Org policy reuses the policy engine.** `Policy` and `Rule` from `seatbelt.policy.engine`, built from YAML. New built-in rules `models(*allowed)` and `max_output_tokens(n)` on the request; `denylist` on `tool_use` blocks the model returns. A denied request records a `policy.check` and answers 403 with the reason. A denied tool call is recorded and the response still relayed (the gateway cannot stop a client running a tool locally), but the next request whose history carries a `tool_result` for that call is refused: a denied tool cannot feed the model. That is the enforcement point that exists at a proxy.
- **Streaming** relays each upstream SSE chunk as it arrives and reassembles the final message from the accumulated events, as the adapter does for `messages.stream()`. Client disconnect mid-stream records what was received with `stop_reason: null`.
- **Failures are recorded, never fabricated.** Upstream 4xx/5xx relayed as is and recorded as `model.response` with `error`. Redaction runs before hashing as today, so a secret pasted into a prompt never lands in clear.
- **A launcher for existing CLIs**, `seatbelt run <cli> [args...]`, so nobody configures a client by hand. It reads the gateway URL and the employee's key from `~/.config/seatbelt/gateway.toml` (0600), sets the base URL and auth variables the target CLI honours (for `claude`: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` which is sent as `Authorization: Bearer`, and `ANTHROPIC_CUSTOM_HEADERS` with `X-Seatbelt-Run: <name>`; for `codex`: `OPENAI_BASE_URL` and `OPENAI_API_KEY`), spawns the CLI with the inherited TTY, and on exit sends `X-Seatbelt-Run-End`. Nothing else: no wrapping of the CLI's I/O, no hooks. Everything the agent does reaches the model as message history, which the gateway records.
- **Fleet report**, `seatbelt report <runs dir> [--json]`: by principal, model and tool with call counts, token totals, policy denials, failed and unattested runs. Read-only, same reader as `pack`.
- **Optional extra `gateway`** on `starlette`, `uvicorn`, `httpx`; all already ship with the SDK extras. A Docker image for the gateway ships with 0.2.0, since a central service needs one.

Rejected: a local per-machine proxy (record on the employee's disk; identity from the OS user is spoofable), SSO from day one (IdP integration in every client before anything records), one ledger per request (an auditor reconstructs a conversation from many files), client-declared runs only (every client must be configured), our own agent CLI (a second product; employees must switch tools), Claude Code hooks as the capture path (tool-specific; the history already carries every tool call and result).

## Module

- `src/seatbelt/gateway/config.py`: `GatewayConfig` (Pydantic, `extra="forbid"`), principals, upstreams, policy, loaded from YAML; `keygen`.
- `src/seatbelt/gateway/sessions.py`: `Sessions` table (`(principal, run) -> Recorder + lock + last activity`), idle sweeper, startup close of open chains.
- `src/seatbelt/gateway/app.py`: Starlette app; auth, routing by path prefix, policy, forward via `httpx.AsyncClient`, relay, record. `serve` runs uvicorn.
- `src/seatbelt/gateway/formats/{anthropic,openai_chat,openai_responses,gemini}.py`: `Format` protocol: `model(request)`, `record_request`, `record_response(json | sse events)`, `tool_calls(response)`, `tool_results(request)`. `adapters/anthropic.py` becomes a thin caller of `formats/anthropic.py`.
- `src/seatbelt/gateway/launcher.py`: `seatbelt run`; per-CLI env presets for `claude`, `codex`, `gemini`, plus `--env` passthrough for anything else.
- `src/seatbelt/report/fleet.py`: aggregation and table.
- `Event` schema: `run.start` gains the `principal.*` and `client.*` attrs (attrs only, no field change, no schema version bump).

## Config

```yaml
listen: 0.0.0.0:8080
signing_key: /etc/seatbelt/gateway.key
ledgers: /var/lib/seatbelt/runs
session_idle: 15m
upstreams:
  anthropic: { url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY }
  openai:    { url: https://api.openai.com,    key_env: OPENAI_API_KEY }
policy:
  models: [claude-sonnet-5, claude-opus-5, gpt-5]
  tools_denied: [run_shell, send_email]
  max_output_tokens: 16000
principals:
  - { id: alice@corp, key_sha256: "9f2c...", issued: 2026-09-18 }
```

## CLI

- `seatbelt gateway serve --config gateway.yaml`
- `seatbelt gateway keygen --user <id> --config gateway.yaml`
- `seatbelt run <cli> [args...]` (reads `~/.config/seatbelt/gateway.toml`)
- `seatbelt report <runs dir> [--json]`

## Tests

No live calls. Starlette's test client drives the app; `httpx.MockTransport` plays upstream with recorded fixtures per format, SSE synthesised as `test_anthropic_transports.py` does. Covers: auth (unknown key 401, nothing written), session continuity and idle close, named runs, per-session serialisation, policy denial on request and on tool result, streaming relay and reassembly, client disconnect, upstream error relay, startup close of an open chain, launcher env presets (spawn `env` and inspect), fleet report totals. Property test: any interleaving of sessions yields chains that verify. Every gateway ledger passes `verify --pubkey` and `pack`.

## Docs

ADR 0006 (gateway), ADR 0007 (launcher), README "Run it for a team", `docs/overview.md`, CHANGELOG, ROADMAP 0.2.0, `docker/Dockerfile.gateway`.

## Phasing

0.2.0: gateway with Anthropic Messages and OpenAI Chat, sessions, identity, policy, launcher for `claude` and `codex`, fleet report, Docker image. 0.3.0: OpenAI Responses, Gemini, SIGHUP config reload, central sink (ship ledgers to object storage at run end), OIDC auth for Claude Desktop and SSO lookup, and a Compliance API importer (`seatbelt import compliance`) that polls Anthropic's Team/Enterprise session transcripts, Cowork included, into signed ledgers for tenants whose desktop clients are not gateway-routed.

## Coverage by client

| Client | Route | Phase |
|---|---|---|
| Claude Code | `seatbelt run claude` or `ANTHROPIC_BASE_URL` | 0.2.0 |
| Claude Desktop, Cowork | MDM or in-app gateway config | 0.2.0 |
| Claude SDKs, Agent SDK | base URL | 0.2.0 |
| Codex, Cursor, LangChain, OpenAI SDKs | `OPENAI_BASE_URL` | 0.2.0 |
| OpenAI Agents SDK (Responses) | base URL | 0.3.0 |
| Gemini CLI, Google SDKs | base URL | 0.3.0 |
| claude.ai web, unmanaged desktop | Compliance API importer | 0.3.0 |

## Out of scope

PII redaction beyond secrets, retention policy, a web UI over sessions, per-tenant multi-org, rate limiting, cost allocation beyond token totals.
