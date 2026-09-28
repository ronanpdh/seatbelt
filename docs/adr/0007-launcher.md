# 7. Launcher: `seatbelt run` presets the environment, nothing more

- Status: accepted
- Date: 2026-09-28

## Context

The gateway records a client only if the client is pointed at it with the employee's key. Asking every employee to set base URLs and tokens by hand is where a rollout fails, and a client left with a real provider key in its environment can bypass the gateway without anyone noticing.

## Decision

`seatbelt run <cli> [-- args]` reads the gateway URL and the employee's key from `~/.config/seatbelt/gateway.toml`, sets the environment variables the CLI honours, and runs the CLI with the inherited terminal. For `claude`: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` (sent as `Authorization: Bearer`) and `ANTHROPIC_CUSTOM_HEADERS` with `X-Seatbelt-Run: <name>`, appended to any headers the user already set. It removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `OPENAI_API_KEY` from what the child inherits. When the CLI exits it ends the named run with `POST /seatbelt/runs/<name>/end`, and exits with the CLI's code.

The launcher ignores Ctrl-C while the CLI runs, because Claude Code uses it to cancel a response and the terminal sends it to both processes; it starts ignoring only after the spawn, since an ignored signal is inherited. It uses only the standard library, so an employee machine needs no server dependencies. It warns when the config file is readable by others.

Rejected: wrapping the CLI's input and output (the model's message history already carries everything the agent did, and the gateway records it), installing hooks into the CLI (tool-specific and editable by the user), a preset for Codex (it speaks only the OpenAI Responses API, which the gateway serves from 0.3.0; `seatbelt run codex` says so rather than launch a client that would not be recorded).

## Consequences

- Nothing on the employee's machine is trusted: the launcher is a convenience, and the gateway enforces identity and policy whether or not it is used.
- A run started by the launcher is closed when the CLI exits, not after the idle window.
- A client the launcher has no preset for is pointed at the gateway by hand, with the same variables.
