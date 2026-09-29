# 7. Launcher: `seatbelt run` presets the environment (and, for Codex, a model provider), nothing more

- Status: accepted
- Date: 2026-09-28

## Context

The gateway records a client only if the client is pointed at it with the employee's key. Asking every employee to set base URLs and tokens by hand is where a rollout fails, and a client left with a real provider key in its environment can bypass the gateway without anyone noticing.

## Decision

`seatbelt run <cli> [-- args]` reads the gateway URL and the employee's key from `~/.config/seatbelt/gateway.toml`, sets the environment variables the CLI honours, and runs the CLI with the inherited terminal. For `claude`: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` (sent as `Authorization: Bearer`) and `ANTHROPIC_CUSTOM_HEADERS` with `X-Seatbelt-Run: <name>`, appended to any headers the user already set. For `codex` (from 0.3.0): `SEATBELT_GATEWAY_KEY`, and a `seatbelt` model provider given as `-c` overrides before the user's arguments (the gateway URL plus `/v1`, the Responses API over HTTP, `env_key = "SEATBELT_GATEWAY_KEY"`, `X-Seatbelt-Run` in `http_headers`), because Codex's built-in provider cannot carry a header. It removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY` and `CODEX_API_KEY` from what the child inherits. When the CLI exits it ends the named run with `POST /seatbelt/runs/<name>/end`, and exits with the CLI's code.

The launcher ignores Ctrl-C while the CLI runs, because Claude Code uses it to cancel a response and the terminal sends it to both processes; it starts ignoring only after the spawn, since an ignored signal is inherited. It uses only the standard library, so an employee machine needs no server dependencies. It warns when the config file is readable by others.

Rejected: wrapping the CLI's input and output (the model's message history already carries everything the agent did, and the gateway records it), installing hooks into the CLI (tool-specific and editable by the user), a preset for Codex in 0.2.0 (it speaks only the OpenAI Responses API, which the gateway did not serve yet; `seatbelt run codex` said so rather than launch a client that would not be recorded). From 0.3.0 the gateway serves the Responses API and `seatbelt run codex` launches Codex with a model provider given on its command line, since its built-in provider cannot carry the run header ([plan](../plans/2026-09-28-responses-format.md)).

Since 0.3.0 the file is `~/.config/seatbelt/config.toml` (`gateway`, `key`), with 0.2.0's `gateway.toml` read only when there is none; with no gateway set, the run is recorded locally. After 0.5.0 it also removes Claude Code's Bedrock and Vertex AI switches and `CLAUDE_CODE_OAUTH_TOKEN`, never passes seatbelt's own secrets (`SEATBELT_SIGNING_KEY`, the sink's credentials) to the CLI, passes SIGTERM and SIGHUP on to it, and exits 128 + N when signal N ended it.

## Consequences

- Nothing on the employee's machine is trusted: the launcher is a convenience, and the gateway enforces identity and policy whether or not it is used.
- A run started by the launcher is closed when the CLI exits, not after the idle window.
- A client the launcher has no preset for is pointed at the gateway by hand, with the same variables.
