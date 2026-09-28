# Changelog

All notable changes to seatbelt are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/). Pre-1.0, minor versions may break the ledger schema; each such change gets a schema version bump and a note here.

## [Unreleased]

### Added
- `seatbelt report` with no runs yet says where it looked and how to start one.
- `seatbelt run claude|codex|gemini` records on your own machine with nothing to set up: the recording gateway starts inside the run on a free localhost port, the CLI keeps its own sign-in (Claude subscription or API key; ChatGPT or API key for Codex; Google account or API key for Gemini CLI), and each run's ledger is signed with a key made on first use and written to the platform's data folder (`$XDG_DATA_HOME/seatbelt` or `~/.local/share/seatbelt`, `~/Library/Application Support/seatbelt`, `%LOCALAPPDATA%\seatbelt`; `SEATBELT_HOME` overrides). The run's key and name travel in the base URL's path, never in headers a CLI also sends elsewhere; Codex gets the key in its environment, off its command line, and sends it to its provider alone. A base URL the CLI already has (`ANTHROPIC_BASE_URL`, `GOOGLE_GEMINI_BASE_URL`, `CODE_ASSIST_ENDPOINT`) becomes where the recorder forwards. `seatbelt report` with no directory reports these runs against this machine's key. Settings, all optional, in `~/.config/seatbelt/config.toml`: `ledgers`, `[upstreams]` and `[sink]`; `gateway` and `key` record through an org's gateway as before. `seatbelt.gateway.local`; guide: `docs/local-recording.md`.
- Gateway upstreams without `key_env` pass the client's own credentials through; the client names its issued key in `x-seatbelt-key` or in a `/_seatbelt/<key>/<run>/` path prefix, and a seatbelt key is never forwarded. Codex's ChatGPT-login traffic (`chatgpt-account-id`) goes to a `chatgpt` upstream's `/backend-api/codex`, and Gemini CLI's Google sign-in (`/v1internal:generateContent`, `:streamGenerateContent`) is recorded through a `codeassist` upstream, its account calls forwarded and any other refused. Gzip and deflate request bodies are recorded and forwarded as sent; others are refused with 415.
- The gateway reloads its config without a restart: when the file's content changes (checked every 30 s, including a change made while it starts) or on `SIGHUP`. `principals`, `policy` and `upstreams` apply from the next request, `session_idle` from the next idle sweep; `listen`, `ledgers` and `signing_key` still need a restart, and a reload that changes them says so and keeps the running values. A file that fails to load is logged once and the running config stays. A deleted or reissued key is refused from the reload on and its open sessions are ended and signed. `Sessions.end_principal`; `Sessions.idle` is settable. `seatbelt.gateway.config.read_config` returns the config with the bytes it came from.

- The gateway serves the OpenAI Responses API (`POST /v1/responses`, streamed or not; `seatbelt.gateway.formats.openai_responses`), as current Codex and the OpenAI Agents SDK call it. Tool calls the client runs (`function_call`, `custom_tool_call`, `local_shell_call`, `shell_call`, `apply_patch_call`, `computer_call`, and a `tool_search_call` the client executes) are recorded and linked to their results by `call_id` (the spec's `local_shell_call_output` by `id`); tools the provider runs (web search and the like) stay in the recorded response. An MCP tool Codex calls (namespace `mcp__<server>`) is named `mcp__<server>__<tool>`, as Codex's hooks and Claude Code name it; any other namespace keeps the bare name. A stream is recorded from its terminal event as sent, or, if it stopped short, rebuilt from what arrived (partial text, arguments and input included) and recorded as not completed; only calls that arrived whole are recorded as calls. `models`, `max_output_tokens` and `tools_denied` apply as for the other formats; an employee's denied calls are remembered across sessions until a restart, for clients that chain `previous_response_id`. A request with `background: true` is refused with 400, since its output would be fetched outside the recorded exchange. Sources for every wire fact: `docs/plans/2026-09-28-responses-format.md`.
- `seatbelt run codex`: launches Codex with a `seatbelt` model provider given as `-c` overrides (the gateway URL plus `/v1`, Responses over HTTP, the key from `SEATBELT_GATEWAY_KEY`, the run name as an `X-Seatbelt-Run` header), and strips `OPENAI_API_KEY` and `CODEX_API_KEY` from its environment.
- The `max_output_tokens` policy rule also caps the Responses API's `max_output_tokens`.
- Each release attaches its SLSA build provenance as `seatbelt-<version>.intoto.jsonl` (the Sigstore bundle from the attestation), beside the attestation GitHub stores, so tools that read release assets, OpenSSF Scorecard among them, find it. Verify with `gh attestation verify <wheel> --bundle seatbelt-<version>.intoto.jsonl --repo ronanpdh/seatbelt`.
- CodeQL analyses the Python code on every push to main, every pull request and weekly; Hadolint lints both Dockerfiles in CI.
- Each release publishes the gateway image to `ghcr.io/ronanpdh/seatbelt-gateway:<version>` and `:latest`, with a signed build provenance attestation pushed beside it (`gh attestation verify oci://ghcr.io/ronanpdh/seatbelt-gateway:<version> --repo ronanpdh/seatbelt`).
- Central sink: with `sink:` in the gateway config, each closed and signed ledger and its signature are uploaded to S3-compatible object storage (Hetzner Object Storage, AWS S3 and the like) in the background, retried with backoff, and anything unshipped is shipped at the next start; `<ledgers>/.shipped/` records what was sent. Requests are signed with SigV4 over the full payload, with no checksum headers and the bucket in the host name. Credentials come from `SEATBELT_SINK_ACCESS_KEY` / `SEATBELT_SINK_SECRET_KEY` (names configurable). `seatbelt.gateway.sink`; `Sessions` takes an `on_close` callback.
- Fuzzing: `fuzz/fuzz_parsers.py` (Atheris, coverage-guided) feeds arbitrary bytes to the ledger reader and chain verifier and to the streaming assemblers, which must only refuse, never crash; CI runs it for 60 s on every push and pull request from seed inputs in `fuzz/corpus/`. Atheris is in its own `fuzz` dependency group, locked with hashes.
- OIDC sign-in at the gateway, for Claude Desktop's `inferenceGatewayOidc`: with `oidc:` in the config, a Bearer token from your identity provider is accepted once its signature (against the provider's keys, from discovery or `jwks_url`, cached 5 min and refetched on rotation), `iss`, `aud` and `exp` check out. Users are identified by `principal_claim` (Entra `oid`, Okta `sub`), sessions are keyed by issuer and user id so a refreshed token continues the ledger, and `run.start` records `principal.auth`, `principal.issuer` and `principal.name`. Only asymmetric algorithms are accepted; an optional `allow` list restricts who may use the gateway. Issued `sbk_` keys keep working beside it. New dependency in the `gateway` extra: PyJWT.
- The gateway serves the Gemini API (`POST /v1beta/models/{model}:generateContent` and `:streamGenerateContent`, SSE or JSON array; also under `/v1` and `/v1alpha`; `seatbelt.gateway.formats.gemini`), as Gemini CLI and Google's Gen AI SDKs call it, through a `gemini` upstream. The issued key is accepted as `x-goog-api-key` or `?key=` and never forwarded. The model is recorded from the URL. Function calls are recorded and linked to their results by id or, where the API gave no id and the client made one up, by tool name and arguments; each result is recorded once however often the history is resent. `countTokens` and the embedding methods are forwarded unrecorded, and any other method, or a model name that is not a plain name, is refused. Errors on Gemini paths are shaped as Google's (`error.code`, `error.message`, `error.status`). Sources: `docs/plans/2026-09-28-gemini-format.md`.
- `seatbelt run gemini`: launches Gemini CLI with `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL` and the run name through `GEMINI_CLI_CUSTOM_HEADERS`; sets `GOOGLE_API_KEY` empty and `GOOGLE_GENAI_USE_VERTEXAI` and `GOOGLE_GENAI_USE_GCA` to `false`, so no `.env` file can switch it away from the gateway. It warns when Gemini CLI's settings (system, user and workspace, trusted or not) may not select API-key sign-in, the only mode that uses the gateway, or may leave its usage statistics, which go to Google directly, switched on.
- The `max_output_tokens` policy rule also caps Gemini's `generationConfig.maxOutputTokens`.
### Changed
- The README leads with `seatbelt run` and is shorter; the attack corpus, sandbox and evidence pack moved to `docs/scenarios.md`.
- The gateway's server packages (Starlette, uvicorn, httpx2, anyio, PyJWT) are core dependencies, since `seatbelt run` starts the gateway to record locally; the `gateway` extra still installs and names the same packages. `seatbelt run --config` defaults to `~/.config/seatbelt/config.toml`, then 0.2.0's `gateway.toml`; with neither, the run is recorded locally rather than refused.
- The Docker base images are pinned by digest, and Dependabot keeps the digests current (Python patch updates only). The gateway image installs its dependencies from `docker/requirements-gateway.txt`, exported from `uv.lock` with hashes and installed with `--require-hashes`; CI fails if the file drifts from the lockfile.
- `seatbelt.gateway.serve.serve` takes the config's path and loads it itself, so the reload watcher compares the file with the exact bytes the gateway started on.
- Gateway sessions are per issued key as well as per principal (`Sessions.get` and `Sessions.end` take the key's hash): a request made with a key just before it was reissued cannot open a ledger that the new key then writes into.
- A tool result is refused while `tools_denied` names the tool the session recorded for its call or the tool the history names, so relaxing the policy by reload takes effect in open sessions too; a result let through that way is recorded as an allowed check.
- A gateway session keeps one format recorder per API rather than per upstream, so Chat Completions and Responses traffic to the same provider keep their open tool calls apart.
- A config with `principals:`, `tools_denied:` or `policy:` left empty (what deleting the last entry leaves) loads as empty.
- The `docker run` steps in the deployment docs mount the config's directory, not the file: `keygen` replaces the file, which a single-file bind mount does not follow.

### Fixed
- `seatbelt gateway serve` stopped by SIGTERM outside a container (systemd, `kill`, `docker run --init`), or by Ctrl-Break on Windows, exited before closing and signing its open sessions, which the next start then recorded as `gateway restarted`: uvicorn raises the signal again after its own graceful stop, and the default handler ended the process. As PID 1 in a container the re-raised SIGTERM was ignored, so the image was not affected. Shutdown also waits for a sweep in progress, and ignores a second stop signal until every session is signed. A start logs, and never signs, a gateway ledger that is closed but unsigned: that looks the same as a ledger rewritten and its signature deleted.

## [0.2.0] - 2026-09-28

### Added
- `Recorder.model_requested`, the request half of `model_call`, for callers that receive the response later.
- Scenario check `no_tool_success: <tool>`: fails on a successful `tool.result` for that tool, so a self-refusal, a policy denial and no call at all pass alike. `trust-exploitation-policy` uses it instead of `policy_denied`, which failed an agent that refused the refund without ever calling the tool.
- `examples/anthropic_scenario_target.py`: Claude as the support agent for the scenario corpus, recorded through the adapter, with a `refund-limit` policy check in the tool executor.
- `seatbelt.gateway.formats.anthropic`: the Anthropic Messages wire format on plain JSON (request, response, SSE reassembly), shared by the SDK adapter and the gateway.
- `seatbelt.gateway.formats.openai_chat`: the OpenAI Chat Completions wire format on plain JSON, including SSE reassembly, for the gateway.
- `seatbelt.gateway.config`: the gateway's YAML configuration (upstreams, policy, session idle, signing key) and issued employee keys stored as SHA-256 hashes; `add_principal` returns a key once and refuses a duplicate id.
- `seatbelt.gateway.app`: the recording gateway (Starlette). Serves `POST /v1/messages` and `POST /v1/chat/completions`, authenticates the employee key (`x-api-key` or `Authorization: Bearer`), swaps in the real provider key, forwards the request body and query string unchanged, relays status, body and headers (`retry-after`, `request-id`), and records request, response and tool calls into the employee's session. `run.start` carries `principal.id`, `principal.key_id` (the first 12 hex digits of the key's SHA-256, so a reissued key is distinguishable), `client.ip` and `client.user_agent`. `X-Seatbelt-Run` names a run; `X-Seatbelt-Run-End: true` or `POST /seatbelt/runs/{name}/end` closes it. Unknown key 401, non-object body 400, unconfigured upstream 404, unreachable upstream 502 (recorded).
- Gateway streaming: an upstream `text/event-stream` response is relayed chunk by chunk and reassembled into one recorded response when the stream ends, however it ends. A stream the upstream breaks is recorded with `upstream stream broke: ...`; one the client leaves, with `stream ended early`; neither records tool calls, whose inputs may be truncated.
- Policy rules `models(*allowed)` and `max_output_tokens(limit)` (checks `max_tokens` and `max_completion_tokens`) for model requests.
- Gateway org policy (`policy:` in the config): a request for a model not in `models`, or over `max_output_tokens`, is refused 403 before it leaves the gateway; every verdict is a `policy.check` on the `model.request`. A tool in `tools_denied` that the model asks for is recorded as denied on its `tool.call` and relayed (the gateway cannot stop a local tool), and any later request carrying that tool's result is refused 403, in the same session or, by the tool name in the history, a later one. Gateway-generated errors use the provider error shape `{"type": "error", "error": {"type", "message"}}`.
- Gateway probe endpoints for Claude Code and Claude Desktop: `HEAD|GET /api/hello` answered locally; `POST /v1/messages/count_tokens`, `GET /v1/models` and `GET /v1/models/{id}` authenticated and forwarded (query string included) but not recorded. Anthropic when the path is under `/v1/messages` or the client sent `anthropic-version` or `x-api-key`, else OpenAI.
- `seatbelt gateway serve [--config gateway.yaml]`: runs the gateway under uvicorn. On start it closes chains a crash left open; a sweeper closes idle sessions every 30 s; on shutdown it drains in-flight requests for up to 30 s and closes every session. `seatbelt gateway keygen --user <id>` issues an employee key, printed once. New extra `gateway` (starlette, uvicorn, httpx2, anyio).
- `seatbelt run <cli> [-- args]`: launches `claude` with the environment preset to use the gateway (base URL, the employee key from `~/.config/seatbelt/gateway.toml`, and for Claude Code an `X-Seatbelt-Run` header), strips real provider keys from the child, leaves Ctrl-C to the child, and ends the named run when it exits. Standard library only, so employee machines need no extra. `codex` is refused with the reason: current Codex speaks only the OpenAI Responses API, which the gateway serves from 0.3.0.
- `seatbelt report <runs> [--pubkey] [--json]` (`seatbelt.report.fleet`): runs, model calls, tokens and policy denials by principal and by model (a refused request counts against the model asked for), tool calls by name, and the failed, incomplete, unattested, forged and broken runs. A ledger whose chain fails is listed as broken and left out of every total. Exit 1 on a broken or forged ledger.
- The gateway's signing key can come from the environment, `SEATBELT_SIGNING_KEY` (PEM or base64 of it), for hosts that inject secrets as env; `signing_key` in the config becomes optional, and setting both is refused. `Signer.from_pem`. Surrounding quotes and any character that is not base64 (whitespace from a wrapped paste, the `%` zsh prints after output with no final newline) are ignored with a logged warning, and a value that still fails is described by its shape (length, stray characters, whether it looks like base64 of a PEM key) without printing any of it.
- `docker/Dockerfile.gateway` builds the wheel itself in a first stage, so a host that builds from git (Coolify and similar) can use it without `uv build` first.
- `seatbelt.gateway.sessions`: one ledger per `(principal, run name)`, opened on first request and closed and signed on idle, explicit end or shutdown, never while a request is in flight (`get`/`release` pairs; `close_all` waits up to a timeout); `close_open_chains` closes chains a crash left open with `run.ok=false` and signs them, and skips (with a logged warning) any ledger it cannot read or whose chain does not verify, so a bad file neither blocks startup nor gets signed.

### Changed
- The Anthropic adapter records every integer `usage` field the provider returns (cache tokens included), not only input and output tokens.

## [0.1.0] - 2026-09-18

### Added
- `docs/overview.md`: what the harness records, why the record proves itself, and how to use every command.
- OpenSSF Best Practices evidence (`docs/openssf-best-practices.md`), a bug-report and test policy in CONTRIBUTING.md, secure-design and cryptography statements in SECURITY.md, a command reference and CI/Scorecard badges in the README.
- Scenario pack (`seatbelt.scenarios`): a shipped corpus under `scenarios/` of adversarial single-turn scenarios, each mapped to the OWASP Top 10 for Agentic Applications (and MITRE ATLAS where one exists), run against a `target(rec, inputs)` callable with `seatbelt scenarios <corpus> --target module:func`. Checks (`no_tool_call`, `tool_call`, `policy_denied`, `no_match`, `run_ok`) are evaluated over the ledger and every finding cites the event ids that prove it. `--list` shows the corpus; `--key` signs each ledger. Schemas in `docs/schema/`. See ADR 0003.
- `examples/scenario_target.py`, a scripted agent with one deliberate flaw so the demo shows a finding.
- `pyyaml` is a dependency.
- Evidence pack (`seatbelt.report.pack`): `seatbelt pack <runs_dir> --out audit.seatbelt.zip [--key] [--corpus]` bundles ledgers, attestation sidecars, `findings.json` and the corpus into one zip with a signed `pack.json` manifest; `seatbelt verify-pack <zip> [--pubkey]` re-checks the signature, every member hash, every chain, every attestation and every finding's evidence offline. Format: `docs/spec/evidence-pack-v1.md`, ADR 0004, schema `docs/schema/pack.json`.
- `seatbelt.attest.manifest.Signed`, the shared base for signed documents; `Signer.sign` accepts any `Signed`.
- Docker sandbox for scenario targets: `seatbelt scenarios ... --image <img> [--target-dir .] [--timeout 120]` runs each scenario in its own hardened container (no network unless the scenario sets `egress: true`, read-only root, no capabilities, host uid) built on `docker/Dockerfile`, and records `sandbox.image`, `sandbox.image_digest` and `sandbox.egress` in `run.start`. The signing key stays on the host. `--list` shows egress. See ADR 0005.
- `Scenario.egress` (default false); `seatbelt.scenarios.runner.record` and `collect` for callers that produce ledgers another way.

### Fixed
- CLI messages are no longer word-wrapped at the terminal width, so a long path or reason stays on one line; the attest test failed in CI on a wrapped line.
- Pre-commit runs ruff on Markdown code blocks as CI does.

## [0.0.4] - 2026-09-17

### Added
- Attestation (`seatbelt.attest`): `Recorder.start(..., signer=Signer.from_file(key))` signs an Ed25519 manifest (final hash, event count, file digest) into `<run id>.attest.json` at run end, failed runs included. `seatbelt keygen` writes the key pair, `seatbelt attest <ledger> --key` signs after the fact, and `seatbelt verify --pubkey` reports attested, FORGED (exit 1), UNCHECKED (sidecar but no key) or unattested. A truncated or rewritten tail, which the chain check alone accepts, is now detected. See ADR 0002.
- `cryptography` is a dependency.

## [0.0.3] - 2026-09-17

### Security
- `Event` and `Actor` forbid unknown keys. A key injected into a ledger line previously survived `verify`, because the canonical form is a re-dump of the parsed model.
- Redaction patterns are anchored to token shapes: `bearer` needs a 16+ character token and `sk-` may not follow a letter, so ordinary prose (`the bearer of`, `desk-...`) is no longer destroyed in the record. Fine-grained GitHub tokens (`github_pat_`) are redacted.
- Ledger files are created mode 0600 and each event is fsynced.
- `Recorder.start` rejects a `run_id` that is not a safe filename and refuses to append to an existing ledger.

### Added
- Policy engine at the tool boundary (`seatbelt.policy.engine`): `Recorder.start(..., policy=Policy(*rules))` evaluates every `Rule` against each `Recorder.tool_call`, records one `policy.check` per rule linked to the `tool.call`, and raises `PolicyDenied` before the tool body runs if any rule denies. A rule that raises counts as a denial (fail closed) and is recorded as one. Built-in rules `allowlist(*tools)` and `denylist(*tools)`. The Anthropic and OpenAI Agents adapters only observe tool calls the framework dispatches, so they record no checks; enforce through `Recorder.tool_call`.
- `run.end` carries `run.error` (exception type and message) when a run fails, and a `model_call` whose body raises records a `model.response` with `error` before re-raising; likewise a `tool_call` whose body raises records a `tool.result` with `error`.
- `seatbelt reconstruct` verifies first: BROKEN (exit 1) on a tampered ledger, a warning on an incomplete one. `run.start` and `run.end` rows now show the harness version and `ok`/`FAILED: <error>`.
- `verify` reports `complete` only when the ledger has exactly one `run.start`, at seq 0.
- `seatbelt.ledger.store.read_events`.
- Anthropic adapter covers every call style: `messages.stream()`, `AsyncAnthropic` via `AnthropicAdapter.async_messages()`, and the beta endpoint by passing `client.beta`. All record the same ledger shape. A stream the caller abandons (break or `close()`) is recorded as what was received, with `stop_reason: null` and no tool calls; the wrapper never reads further, so exits behave exactly like the SDK's.
- Model requests also record `thinking`, `output_config`, `stop_sequences` and `betas`.

### Removed
- `seatbelt.adapters.base.Adapter` protocol and `AnthropicAdapter.attach`: nothing consumed the protocol, and `attach` did not affect wrappers already created.
- `Ledger.read`: use `seatbelt.ledger.store.read_events(path)`.

### Fixed
- `Ledger.append` is thread-safe. Concurrent appends from several threads previously corrupted the chain (sequence gaps).
- `seatbelt reconstruct` raised a traceback on a corrupt or missing ledger.
- `create(stream=True)` raised after recording the request, leaving it unanswered; it now fails up front with a pointer to `.stream()`.

## [0.0.2] - 2026-09-15

### Changed
- **Ledger schema version 1.** Every event now carries a required `schema_version`, and `verify` rejects versions it does not know. Ledgers written by 0.0.1 have no version and are rejected as an old format; they cannot be verified by 0.0.2.

### Added
- Release workflow: tagged builds attach the wheel, sdist and a CycloneDX SBOM to a GitHub release, with Sigstore-signed SLSA build provenance and SBOM attestations.
- `Recorder.tool_called` and `Recorder.tool_returned` primitives for adapters that observe tool calls without executing them; `tool_call` context manager now built on them.
- `gen_ai.tool.call.id` attribute on `tool.call` events (provider's own call id).
- `Adapter` protocol (`seatbelt.adapters.base`).
- Anthropic Messages API adapter (`seatbelt.adapters.anthropic`): records requests, responses with provider-returned model version and token usage, `tool_use` blocks as `tool.call`, and matching `tool_result` blocks as `tool.result`.
- Optional dependency group `anthropic`.
- Recorded fixture `tests/fixtures/anthropic_refund.json` and replay tests; adapter tests skip when the extra is absent.
- `examples/anthropic_refund.py`, a one-tool live example.
- OpenAI Agents SDK adapter (`seatbelt.adapters.openai_agents.SeatbeltProcessor`), a tracing processor: generation and response spans become `model.request`/`model.response`, function spans `tool.call`/`tool.result` (MCP data under `mcp.*`), agent, handoff and guardrail spans `decision` events with the agent as authority.
- Optional dependency group `openai-agents`; `examples/openai_agents_refund.py`, the same refund agent.
- `Recorder.tool_called` accepts extra `attrs` and a `parent_id`; both adapters link each `tool.call` to the `model.response` that requested it.
- `seatbelt verify` reports INCOMPLETE (exit 1) when a chain is intact but does not end in a `run.end` whose `run.events` count matches, catching a truncated tail.
- `examples/anthropic_walkthrough.py`, a live run exercising every event kind.

### Fixed
- `seatbelt reconstruct` rendered ledger text as Rich markup, so bracketed text such as `[authority: ...]` vanished and ledger content could restyle or hide cells. Cells are now literal, and summaries are one line, at most 80 characters.
- Redaction now converts Pydantic models to JSON before redacting. SDK content blocks passed back in the request history were previously neither redacted nor hashable, so the Anthropic adapter's `create` raised.
- `seatbelt verify` reports a corrupt or missing ledger as BROKEN (exit 1) instead of raising.
- The `seatbelt` console script pointed at a missing `seatbelt:main`.
- Package version now comes from `pyproject.toml`.
- CI now actually runs on Python 3.13; previously every matrix leg used 3.12.

## [0.0.1] - 2026-09-14

### Added
- Event model (`Event`, `Actor`, `Kind`) with canonical JSON hashing and a genesis hash.
- Append-only JSONL `Ledger` with a SHA-256 hash chain per run; resumes after restart.
- Redaction of common secret formats before hashing and writing.
- `Recorder` API: run start and end (with failure flag), user message, model call, tool call, policy check, decision (with authority and basis), action, outcome.
- `verify_events` and `verify_file` with a `Verdict` naming the first bad sequence number.
- Timeline reconstruction (`report.timeline`).
- CLI: `seatbelt version`, `seatbelt verify <ledger>` (exit 1 on tamper), `seatbelt reconstruct <ledger>`, `seatbelt demo`.
- Tests: hash determinism, chain link and resume, property test that any edit breaks the chain, deletion detection, redaction, recorder lineage and failure path, CLI round trip and tamper detection.
- Project scaffolding: uv, ruff, pyright strict, pytest, Hypothesis, pre-commit, CI, Dependabot, Scorecard, SECURITY.md, CONTRIBUTING.md, STANDARDS.md, ROADMAP.md.

[Unreleased]: https://github.com/ronanpdh/seatbelt/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.2.0
[0.1.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.1.0
[0.0.4]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.4
[0.0.3]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.3
[0.0.2]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.2
[0.0.1]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.1
