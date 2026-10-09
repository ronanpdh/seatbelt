# Changelog

All notable changes to seatbelt are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/). Pre-1.0, minor versions may break the ledger schema; each such change gets a schema version bump and a note here.

## [Unreleased]

### Added
- **An HTML page for each run** ([design](docs/plans/2026-10-09-html-run-report-design.md)). `seatbelt run` writes `<run id>.html` beside each ledger when it closes and signs it, and prints its path. A killed run's ledger gets its page when the next run closes it. Any ledger can be rendered with `seatbelt reconstruct [run] --html [--out page.html]`.
  - **What it shows:** the run's name, who, the client, start, end and outcome; tokens per model, with the prompt cache beside input; tool calls; policy refusals; errors; and every event, each opening to all that was recorded with it. An event over 64 KiB shows its first 8 KiB and its last 56 KiB, where a request's newest turn is; the ledger holds the rest.
  - **A view, not evidence.** It says what was checked when it was made (chain, completeness, signature, final hash, the ledger's SHA-256) and the command that checks the ledger again. A ledger that is broken, forged, or unsigned when a key is given gets no page.
  - **Safe to open.** Every value from the ledger is escaped, and control characters and bidirectional overrides are shown, not obeyed. The page has no script and loads nothing, and a Content-Security-Policy says so. Mode 0600, written whole.
  - `html = false` in `~/.config/seatbelt/config.toml` turns it off. A page that cannot be written is a warning; the run still ends signed.
  - `seatbelt erase` removes a person's pages with their ledgers, and counts them in its record (`erasure.pages`). `pack` and the sink leave pages out.
- New dependency: Jinja2 (`>=3.1.6`, the first release with no known advisory).

## [0.6.0] - 2026-10-09

### Added
- **`seatbelt run` shows that it is recording** ([#46](https://github.com/ronanpdh/seatbelt/issues/46), [ADR 0008](docs/adr/0008-visible-seatbelt.md)):
  - Every line it prints starts with `[seatbelt]`, shown as a bold, reversed `seatbelt` badge on a terminal, its log warnings and errors included.
  - A seatbelt buckles as the CLI starts, as issue #46 drew it: a belt 34 rows by 121 columns whose tongue slides into the buckle, then CLICK, in about 2 seconds. seatbelt's lines follow below it. It unbuckles, in under a second, when the CLI exits. A terminal too small for it gets a line of belt instead. `SEATBELT_NO_ANIMATION=1` turns both off.
  - Claude Code's status line ends with seatbelt's badge and what it is doing (`recording`, or `recording · org policy` under a gateway's policy), after your own status line and a `|`. `seatbelt run claude` passes it through `--settings`, merged with a `--settings` of your own.
- **Through a gateway, `seatbelt run` checks the gateway and your key before the CLI starts,** with the new `GET /seatbelt/policy`, and prints the policy, e.g. "the org's policy denies Bash, Write". A gateway it cannot reach, or one that refuses your key, stops the run with exit 1, instead of the CLI failing once it has started. A 0.5.3 or earlier gateway answers 404, and the run goes on as before.
- **A tool the policy denies stops before it runs in Claude Code.** `seatbelt run claude` gives Claude Code a PreToolUse hook for each denied tool. The gateway accepts exactly that refusal as the tool's result, and records it as an allowed check ("stopped before it ran"). It still refuses a result from a tool that ran. A user can turn the hook off (`disableAllHooks`, which seatbelt warns about); the gateway's refusal stays the control.
- The gateway names the run that recorded a request in an `X-Seatbelt-Run-Id` response header: the ledger's file name without `.jsonl`. A client that names its run with `X-Seatbelt-Run` could not tell which ledger held its call, since the run id ends in random hex. The header is on relayed responses, JSON and streamed, on policy refusals and on the 502 for an unreachable upstream, and replaces any upstream header of the same name. Responses sent before a run is opened carry none ([docs/deploy/gateway.md](docs/deploy/gateway.md)).

### Changed
- **Policy refusals say what to do.** The message names what the policy allows, and for Claude Code where to fix it: `/model`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, or `/clear` and `/rewind` to leave a conversation that holds a denied tool's result. The ledger records the reason alone, as before.
- **Claude Code gets a policy refusal as 422, not 403.** It read a 403 as a sign-in failure: it retried, then showed "Authentication error" without the gateway's message. Other clients still get 403.

## [0.5.3] - 2026-10-01

### Fixed
- `seatbelt run claude`, recording on this machine, keeps Claude Code's MCP tool search on: it sets `ENABLE_TOOL_SEARCH=true` when the recorder forwards to Anthropic, unless you set it yourself. Behind any other base URL Claude Code turns tool search off and sends every MCP tool's schema with every request, so a run with many MCP tools used much more of its context than the same run without seatbelt. Behind a base URL of your own, or an `[upstreams]` proxy, it is left to Claude Code as before ([docs/local-recording.md](docs/local-recording.md)).
- `seatbelt run` prints the ledger's path quoted, ready to paste into a shell. On macOS it is under `Application Support`, and a shell split the unquoted path at the space into two files that do not exist.
- `seatbelt report` shows Anthropic's prompt-cache tokens, `cache read` and `cache write`, beside `in`. Anthropic's `input_tokens` leaves them out, and with caching they are most of a long prompt, so `in` alone looked small. `--json` has them as `cache_read_input_tokens` and `cache_creation_input_tokens`.

### Security
- The `pyjwt` floor is 2.15.0. The `lowest` workflow's audit reported GHSA-42vr-xj54-vc7v (CVE-2026-101918) against 2.14.0: a `RecursionError` denial of service in PyJWT's payload parse before verification, fixed in 2.15.0. The locked pyjwt (2.15.1) is not affected.

## [0.5.2] - 2026-09-29

### Changed
- Dependency floors are the lowest releases the tests pass with, and never below the first release from which no later one has a known advisory. So `seatbelt-ai` now installs beside frameworks that cap them, such as CrewAI 1.x, which could not be installed with 0.5.1:
  - pydantic 2.12.2, rich 13.8, typer 0.17, uvicorn 0.34 and pyyaml 6.0.2;
  - on the gateway's security path: anyio 4.14.2, cryptography 50.0.0, httpx2 2.12.0, pyjwt 2.14.0 and starlette 1.3.1;
  - the extras: anthropic 1.0.0 and openai-agents 0.19.
- A new `lowest` workflow runs the tests at those floors on Python 3.12 and 3.13, and audits them with `pip-audit`, on every push and pull request and weekly.
- Dependabot updates `uv.lock` only (`versioning-strategy: lockfile-only`); floors move by hand.
- The gateway image builds its hash-pinned requirements from `uv.lock` and its wheel with the build backend inside uv, offline, in a build stage with uv pinned by digest. `docker/requirements-gateway.txt`, `docker/requirements-build.txt` and CI's drift check are gone, so a Dependabot PR no longer needs a hand-made commit.

### Added
- `docs/BUILD.md`: how each release from 0.0.1 to 0.5.1 was built, where it differs from the original build plan, and commands that check it. `log/`: a learning-log entry per release.

## [0.5.1] - 2026-09-29

A review of the whole project, with every finding checked by a second reader. Upgrade gateways promptly.

### Security
- **Gateway probes.** A model lookup (`GET /v1/models/{id}` and the Gemini equivalents) and a Code Assist operation read are forwarded only when the id has the expected shape, and the path sent upstream is built from it. Before, an authenticated employee could reach other provider endpoints through those routes with the gateway's provider key, unrecorded. `GET` and `HEAD` carry no body upstream, and method-override headers are never forwarded.
- **Request shapes the gateway could not record faithfully are refused with 400:** Gemini and Code Assist fields in their snake_case spelling (which escaped the record, the output-token cap and the tool denylist), Gemini `candidateCount` above 1, Chat Completions legacy function calling (`functions`, `function_call`, `function` messages) and `n` above 1. `Format.unrecordable` names the reason.
- **The output-token cap** reads an integral float or an integer in a string as the number it is, and refuses any other value instead of ignoring it.
- **Redaction** also removes seatbelt's own `sbk_` keys, Google API keys, AWS `ASIA` keys, private key blocks, JWTs, Stripe and Slack tokens, passwords in URLs and `…KEY=`, `…TOKEN=`, `…SECRET=` and `…PASSWORD=` lines as in `.env` files (an upper-case name and a value of 16 characters or more), dict keys, and the actor ids and `parent_id` of every event (in `Ledger.append`, so no caller can skip it). Keys that collide after redaction keep both values (`#2`).
- **`verify-pack --pubkey`** fails an unsigned pack and a run without its signature, as `verify --pubkey` does. Pack reading is bounded (sizes, compression ratio, one streamed pass), refuses encrypted or unusual members and run ids that are not plain names, and reports a hostile pack as FORGED instead of crashing.
- **`seatbelt pack`** refuses a signature file that contradicts its ledger and, with `--key`, one another key made, so a pack signature never covers a sidecar the signer did not check. It also refuses a ledger whose file name is not a run id.
- **Without a key,** `verify`, `verify-pack` and `report` compare a signature file with its ledger and fail on a mismatch. This is a consistency check, not tamper evidence: only a key proves a ledger unchanged.
- **Terminal output** shows control characters in ledger and pack text as visible escapes (`seatbelt.terminal.printable`), in the tables, verdicts and errors of `reconstruct`, `report`, `runs`, `verify`, `verify-pack` and `erase`, so recorded text cannot move the cursor, hide lines or write the clipboard.
- **Sandbox:** a scenario's ledger is taken from the container only as a plain file of at most 64 MiB, never through a link. A results folder inside `--target-dir` is hidden from the container.
- **`seatbelt run`** keeps seatbelt's own secrets (`SEATBELT_SIGNING_KEY`, the sink's credentials) out of the CLI's environment, and through a gateway also removes Claude Code's Bedrock and Vertex AI switches and `CLAUDE_CODE_OAUTH_TOKEN`, which would take it around the gateway.
- **Gateway logs:** keys in `?key=` are redacted from the access log, the `/_seatbelt/<key>/` path is served only by the local recorder, and HTTP client request lines are no longer logged. Sign-in errors caused by the identity provider no longer show its URLs or network errors to clients.
- **Release workflow:** the job that builds, attests and uploads the release never installs the dev dependencies or runs the tests, which run in their own read-only job. The tagged commit must be on `main`, uv is pinned by version everywhere and by checksum in the job that builds the release, the gateway image pins its build backend by hash, and the GHCR image (`:latest` included) is pushed only after the PyPI approval.

### Fixed
- A request body over 64 MiB as sent is refused with 413, one that inflates past 64 MiB with 415, and one nested more than 128 levels deep with 400.
- An identity-provider outage no longer signs every Claude Desktop user out: the keys held stay in use for up to an hour, a failed fetch is retried at most every 30 seconds, and one request fetches while the others go on.
- A streamed response the gateway cannot assemble is recorded as the text received, with the error, not as an empty response. A lone surrogate in a model's tool arguments is kept as the text `\ud800` instead of losing the event.
- Streamed Anthropic responses keep thinking, signatures and citations; streamed Chat responses keep refusals; GPT-5 custom tool calls in Chat Completions are recorded and checked. A stream that ends without a stop reason, or with a provider error, is recorded with an error. Gemini tool results are matched to calls whose arguments held a redacted secret.
- A failed write rolls the ledger back to its last whole line. One ledger that fails to close no longer leaves the other sessions in the same sweep open. Two processes never both close the same dead ledger (a lock in `.closing/`), and a local run closes only ledgers that name a run, as local runs' do, whose run is gone: never an erasure record, or a ledger whose run name is not a plain name.
- Signatures and keys are written atomically and fsynced, as is a new ledger's folder entry.
- The sink sends `Content-MD5` (S3 requires it with Object Lock), signs the host it actually sends, refuses an endpoint with a path, query or user name, and accepts `http://` only with `allow_http: true`. A ledger that keeps failing goes to the back of the queue with an error, and a signature added after its ledger shipped is shipped at the next start.
- `seatbelt report` fails on a signature whose ledger is gone (`missing`) and, with `--pubkey`, on a ledger that ended without a signature (`unsigned`), and never adds forged or unsigned ledgers to the totals. An emptied ledger is no longer skipped. `--json` has the new `unsigned` and `missing` lists.
- `seatbelt erase` names, and exits 1 for, an unreadable ledger that is or may be the person's, instead of reporting success. `.erased` is created mode 0600.
- The Compliance importer re-imports a message a killed or failed run may have left partly written, reports a local session it cannot read again for 3 runs as needing a person without stopping the other sources, and refuses a chat returned as partial (`has_more`).
- The Anthropic SDK adapter records a streamed response when the caller's code raises inside `stream()`. The OpenAI Agents processor no longer keeps every agent span.
- `seatbelt run` prints whether it records locally or through a gateway, says afterwards whether a gateway run was recorded (or was not open, which also happens after the gateway closed it for going idle), warns when Claude Code's settings switch it to Bedrock or Vertex AI and when a `gateway.toml` is ignored beside a `config.toml`, exits 128 + N when signal N ended the CLI, passes SIGTERM and SIGHUP on to the CLI, and prints a key or spawn error as one line.
- `seatbelt demo` prints the commands that check and replay its run.

### Changed
- Docs: the library install (`pip install "seatbelt-ai[anthropic]"`), the employee config in `config.toml` everywhere, the gateway image by `<version>` and digest, `FORWARDED_ALLOW_IPS` behind an appending proxy, what a local signature does and does not prove, the scenarios quick start with `--target-dir examples/`, and SECURITY.md's scope (the gateway, its image, the importer and the release workflow).

## [0.5.0] - 2026-09-29

### Added
- Releases are published to PyPI as **`seatbelt-ai`**: `uv tool install seatbelt-ai`, or once with `uvx --from seatbelt-ai seatbelt …`. The module and the command stay `seatbelt`. `seatbelt` on PyPI is an unrelated project that installs the same module and command, so `pip install seatbelt` and `uvx seatbelt` fetch it, not this. The release workflow's new `pypi` job publishes the wheel and sdist the `release` job built, through Trusted Publishing in the `pypi` environment, with no stored token; PyPI attaches its own attestations.

### Changed
- The distribution is renamed `seatbelt` → `seatbelt-ai` (`[tool.uv.build-backend] module-name = "seatbelt"` keeps the module). `seatbelt.__version__` is read from `seatbelt-ai`.
- The release's provenance asset is `seatbelt_ai-<version>.intoto.jsonl`, named like the wheel and sdist. Verify with `gh attestation verify <wheel> --bundle seatbelt_ai-<version>.intoto.jsonl --repo ronanpdh/seatbelt`.
- Upgrading an install from git: `uv tool uninstall seatbelt`, then `uv tool install seatbelt-ai`; the old tool owns the `seatbelt` command, so the new one will not install beside it. Rebuild any `seatbelt-target` sandbox image (docs/scenarios.md); one built before this has no `seatbelt-ai` package, and `seatbelt scenarios --image` now points to the rebuild.
- README links and the cover image are absolute GitHub URLs, so they work on PyPI.

## [0.4.0] - 2026-09-29

### Added
- `seatbelt erase` removes every ledger recorded under a person's principal ids (`--principal`, or `--person` with a people file), with each signature and shipped mark, inside a signed `erasure-…` record that holds only hashes, the `--case` reference, who ran it and counts. A dry run is the default; `--yes` erases. Nothing may be writing: it holds each folder's lock, and refuses while a gateway, an import or a local run is using the folder. An interrupted erase is finished by the next. In importer folders it removes the person's state entries and adds them to `.erased`, so the importer never brings them back; an unreadable `.erased` stops the import. The sink's copies are listed, not deleted. `seatbelt.erase`; guide: `docs/deploy/erasure.md`.
- `seatbelt report --people people.yaml` counts each person once across their principal ids: an issued gateway key, an identity provider's subject, an Anthropic user id from the importer. The file lists each person's ids. Nothing is matched by e-mail, and an id listed for two people is refused. The report lists the ids it joined (`people` in `--json`). `seatbelt.report.fleet.People`.
- `seatbelt report` takes several runs folders, e.g. the gateway's and the Compliance API importer's.
- `seatbelt reconstruct` shows an imported answer's text instead of `(? out)`, and marks imported messages that are not verified content: `[unverified]`, `[marker]`, `[unavailable: <reason>]`.
- The OpenSSF Best Practices badge (bestpractices.dev project 15081) in the README; `docs/openssf-best-practices.md` names CodeQL among the static analysis tools.

### Changed
- `gateway serve` holds `<ledgers>/.lock` while it runs, so `seatbelt erase` never removes a ledger from under it; a second gateway on the same folder now refuses to start. The file-lock helpers moved to `seatbelt.locks`, and `seatbelt.gateway.local.login_name` is public.
- `Recorder.action` and `Recorder.outcome` take extra `attrs`.

## [0.3.0] - 2026-09-29

### Added
- OIDC sign-in from Claude Desktop tested end to end with an Auth0 tenant: sign-in, a reply through the gateway, and a ledger recording the user by their Auth0 `sub`, issuer and email. `docs/deploy/claude-desktop-gateway.md` gains an Auth0 section: a Native app, a fixed loopback callback port, the issuer with its trailing slash. It also covers opening the in-app configuration through Developer Mode. Silent token refresh with Auth0 was not yet confirmed.
- `seatbelt import compliance` imports a Claude Enterprise organization's transcripts from Anthropic's Compliance API into signed ledgers: claude.ai chats, and Cowork, Claude Code, Claude for Microsoft 365, Claude Science and Claude in Chrome sessions, on users' machines and (Cowork) in the cloud. Run on a schedule, it imports each conversation once it has been quiet for an hour, and a conversation that continues gets a further ledger of only its new messages, chained to the last. Each ledger names its source and the endpoint it came from, and each event carries the API's message id and timestamp; no `model.request` is written, since the API returns the conversation, not the requests. Configured by a `compliance:` block in the gateway config (the key from `ANTHROPIC_COMPLIANCE_ACCESS_KEY`), signed with the gateway's key, written to `compliance/` in its ledgers folder, and shipped by the sink when one is set. Follows the API's documented paging and retry contract. **Tested against a fake of the documented API only, not a live tenant.** `seatbelt.compliance`; guide: `docs/deploy/compliance-import.md`; design and sources: `docs/plans/2026-09-29-compliance-importer-design.md`.
- `Recorder.model_responded` records a model's answer seen with no request; `user_message` and `tool_returned` take extra `attrs`, and `tool_returned` a result whose call is not in the ledger.
- `seatbelt report` leaves imported answers marked unverified or unavailable (`compliance.provenance`) out of model calls, and groups answers with no model as `unknown`.
- `seatbelt runs` lists this machine's runs by name, newest first. `seatbelt verify` and `seatbelt reconstruct` take a run's name (`claude-99ce72ff`) or id as well as a path, default to the latest run, and check a local run against this machine's key. `seatbelt run` ends by printing the run's name and the command that replays it, so no path (with its space, on macOS) has to be copied.
- A local run holds an OS lock on `runs/.running/<run>.lock` while it records. Each run closes and signs, in the background, the ledgers that runs killed earlier left open (their lock is free), and leaves live runs' ledgers alone. `close_open_chains` takes `only` and `reason` for this.
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
- Gateway: `GET /v1/models/ft:...` (an OpenAI fine-tuned model id has colons) went to the Gemini upstream; only a `POST /v1/models/{model}:{method}` is Gemini's now.
- Gateway: a pass-through credential was dropped when it merely contained `sbk_` anywhere; a seatbelt key is now recognised by its prefix only.
- Gemini: a tool result that matched no call when first seen (an old one, in a history resent to a new session) could later be recorded as the answer to a new call to the same tool; after its first sighting it matches by exact id only.
- `seatbelt report` with a bad `config.toml` printed a traceback; it now prints the error and exits 1.
- `seatbelt run` crashed where the user has no login name (a container with an arbitrary uid); the run is recorded as `uid-<n>`.
- A local run with a sink configured no longer reads every unshipped ledger before starting the CLI; that now happens in the background.
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

[Unreleased]: https://github.com/ronanpdh/seatbelt/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.6.0
[0.5.3]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.5.3
[0.5.2]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.5.2
[0.5.1]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.5.1
[0.5.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.5.0
[0.4.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.4.0
[0.3.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.3.0
[0.2.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.2.0
[0.1.0]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.1.0
[0.0.4]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.4
[0.0.3]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.3
[0.0.2]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.2
[0.0.1]: https://github.com/ronanpdh/seatbelt/releases/tag/v0.0.1
