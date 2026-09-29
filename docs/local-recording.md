# Recording on your own machine

`seatbelt run claude` (or `codex`, or `gemini`) records the CLI's model traffic on your machine, with nothing to set up. The CLI signs in as it always does. Each run's ledger is signed and written to your local data folder. What that signature does and does not prove is under [Limits](#limits).

```sh
seatbelt run claude      # when it exits: "recorded run claude-99ce72ff"
seatbelt reconstruct     # replay the latest run; or: seatbelt reconstruct claude-99ce72ff
seatbelt runs            # your runs by name, newest first
seatbelt report          # your local runs, checked against this machine's key
```

`verify` and `reconstruct` find a local run by its name, by its id (the file name, with or without `.jsonl`), or take a file path. A local run is checked against this machine's key without `--pubkey`.

To record through your organisation's gateway instead, see [Through a gateway](#through-a-gateway).

## How it works

`seatbelt run` starts the recording gateway inside its own process, on a free port on `127.0.0.1`. It points the CLI at that gateway and stops it when the CLI exits. The gateway forwards every request unchanged to the provider, with the CLI's own credentials, and records the request and the response. Credentials are never recorded.

The CLI's base URL carries a random key and the run's name in its path: `http://127.0.0.1:<port>/_seatbelt/<key>/<run>`. The gateway takes that prefix off before it forwards the request.

- **The key keeps other processes out.** Any other process on the machine can reach the port, but without the key its model requests get a 401 and nothing is recorded.
- **The key is in the path, not a custom header,** because CLIs also send their custom headers to other hosts. Claude Code, for one, sends them on a direct request to `api.anthropic.com` [R1].
- **Codex is the exception.** It takes its provider on the command line, which any user on the machine can read. So Codex gets the key in its environment, which only you can read. Codex sends it as an `x-seatbelt-key` header, to its model provider only (`env_http_headers`) [R3]. Claude Code and Gemini CLI get their base URL, key included, in their environment too.

When the CLI exits, its ledger is closed and signed, and the path is printed. The signing key is made on first use in `keys/` in the data folder, mode 0600, with its public key beside it.

| Platform | Data folder |
|---|---|
| Linux | `$XDG_DATA_HOME/seatbelt`, else `~/.local/share/seatbelt` |
| macOS | `~/Library/Application Support/seatbelt` |
| Windows | `%LOCALAPPDATA%\seatbelt` |

`SEATBELT_HOME` overrides all of these.

## Which sign-ins are recorded

| CLI | Sign-in | Recorded | What `seatbelt run` sets |
|---|---|---|---|
| Claude Code | Claude subscription (Pro, Max, Team, Enterprise) | yes [R1][R2] | `ANTHROPIC_BASE_URL` |
| Claude Code | API key or `apiKeyHelper` | yes [R2] | `ANTHROPIC_BASE_URL` |
| Claude Code | your own LLM gateway (`ANTHROPIC_BASE_URL` with `ANTHROPIC_AUTH_TOKEN`) | yes, then on to your gateway | `ANTHROPIC_BASE_URL`; the recorder forwards to the URL you had |
| Codex | ChatGPT | yes [R3] | a `seatbelt` model provider on the command line, using Codex's own login |
| Codex | API key | yes [R3] | the same provider |
| Gemini CLI | API key | yes [R4] | `GOOGLE_GEMINI_BASE_URL` |
| Gemini CLI | Google account | yes [R4] | `CODE_ASSIST_ENDPOINT` |
| Gemini CLI | Vertex AI | **no** | the base URLs above, which Vertex AI ignores [R4]; `seatbelt run` warns |

How each sign-in is routed:

- **Claude Code on a subscription** keeps its subscription when only the base URL is changed [R1]. The gateway forwards its `anthropic-beta` header unchanged, which a subscription needs [R1].
- **Codex with ChatGPT** sends its model requests to ChatGPT's backend, not the public API. The gateway recognises them by the `chatgpt-account-id` header Codex sends with a ChatGPT login [R3]. It forwards them to `https://chatgpt.com/backend-api/codex/...`.
- **Codex with an API key** goes to `https://api.openai.com/v1/...`.
- **Gemini CLI with a Google account** talks to Google's Code Assist service (`/v1internal:generateContent` and `:streamGenerateContent`). The gateway unwraps each request and records the conversation inside it [R4].
  - Forwarded unrecorded: Gemini CLI's account, quota and settings calls (`loadCodeAssist`, `retrieveUserQuota` and the like), its token counts, its usage metrics (`recordCodeAssistMetrics`), and reads of a long-running onboarding operation.
  - Refused: any other call.
- **A base URL you already had** is where the recorder forwards: `ANTHROPIC_BASE_URL`, `GOOGLE_GEMINI_BASE_URL` or `CODE_ASSIST_ENDPOINT` in your environment, your company's LLM gateway, say. Its credentials go on with it. `[upstreams]` in the settings overrides it.
- **Gemini CLI with nothing selected** needs `security.auth.selectedType` set in `~/.gemini/settings.json`. Otherwise the base URL makes it pick a sign-in mode it refuses to run [R4]. `seatbelt run gemini` warns about this.

## What is not recorded

These requests go from the CLI straight to the provider, not through the recorder, so they are not in the ledger. Most are sign-in, settings and telemetry. Two carry tool traffic: Claude Code's claude.ai connectors (`mcp-proxy.anthropic.com`) and Codex's ChatGPT MCP (`chatgpt.com/backend-api/ps/mcp`). What a tool returns reaches the ledger only as part of the next model request that carries it.

| CLI | Requests that bypass the recorder |
|---|---|
| Claude Code | Seen in the runs: `api.anthropic.com` feature flags, startup bootstrap, the fast-mode check, the MCP registry and event logging. From its docs: `platform.claude.com` sign-in refresh, `claude.ai`, `mcp-proxy.anthropic.com` (connectors), the WebFetch domain check and Datadog log intake [R2] |
| Codex | `auth.openai.com`: sign-in refresh. `chatgpt.com/backend-api`: account check, user settings, plugins, MCP and analytics. `ab.chatgpt.com`: metrics. `github.com` and `api.github.com`: the plugins list [R3] |
| Gemini CLI | `oauth2.googleapis.com` and `www.googleapis.com`: sign-in and user info; `play.googleapis.com`: usage statistics, unless `privacy.usageStatisticsEnabled` is `false` [R4] |

Compressed request bodies (gzip or deflate) are recorded and forwarded as they were sent. A body in an encoding the gateway cannot read is refused with 415, not forwarded unrecorded.

## Settings

Put settings in `~/.config/seatbelt/config.toml`. Every setting is optional.

```toml
ledgers = "~/seatbelt/runs"          # where local runs go (default: runs/ in the data folder)

[upstreams]                          # a provider's URL, e.g. your company's LLM proxy
anthropic = "https://llm-proxy.corp.example"

[sink]                               # also ship each signed ledger to object storage
url = "https://fsn1.your-objectstorage.com"
bucket = "ledgers"
region = "fsn1"
```

- **`[upstreams]`** keys are `anthropic`, `openai`, `chatgpt`, `gemini` and `codeassist`. They override a base URL taken from your environment.
- **`[sink]`** takes the same settings as the gateway's sink ([deploy/gateway.md](deploy/gateway.md#shipping-ledgers-to-object-storage)). It reads the credentials from `SEATBELT_SINK_ACCESS_KEY` and `SEATBELT_SINK_SECRET_KEY`.
  - At exit, the run waits up to 30 seconds for the upload.
  - Any closed ledger not shipped by then is shipped by the next run.

## Through a gateway

To record centrally, give the gateway's URL and your issued key:

```toml
gateway = "https://gw.corp.example"
key = "sbk_..."
```

With a gateway set, `seatbelt run` behaves as before:
- The CLI uses the gateway, with your issued key.
- The gateway holds the provider keys.
- The CLI's own provider credentials are removed from its environment.

A 0.2.0 `~/.config/seatbelt/gateway.toml` with `url` and `key` is still read when there is no `config.toml`. See [deploy/gateway.md](deploy/gateway.md) to run a gateway.

## Limits

- **A run that is killed leaves its ledger open until the next run.**
  - While it runs, each run holds an OS lock on a file in `runs/.running/`, and the OS releases the lock when the process dies.
  - Each run, in the background, closes and signs the open ledgers of runs whose lock is free, recording that the run was killed. It leaves the ledgers of runs still going alone.
  - Until then, `seatbelt report` lists the killed run as incomplete and unattested, and `seatbelt verify` checks its chain and reports it incomplete.
- **The recorder serves only the APIs the gateway records:**
  - Anthropic Messages;
  - OpenAI Chat Completions and Responses;
  - ChatGPT's Codex backend;
  - Gemini `generateContent`;
  - Code Assist.
- **Only a restricted set of requests is forwarded unrecorded.** A CLI feature that calls another model endpoint through the base URL gets an error rather than going unrecorded. Only token counts, model lists, Gemini's embeddings and Code Assist's account calls pass through unrecorded.
- **A local signature does not protect a run from the agent it records, or from you.**
  - The signing key is in `keys/` in the data folder, readable by your user. The CLI, and every tool it runs, runs as your user too.
  - So an agent that can run commands can rewrite a ledger and sign it again, and `seatbelt verify` then reports it attested.
  - What a local signature does show: the file has not changed since it was signed, by another user on the machine or by a later accidental edit.
  - When the agent itself is the adversary, record through your organisation's gateway, which keeps the key and the ledgers on another machine. Or ship each ledger to a `[sink]` bucket with Object Lock in COMPLIANCE mode ([deploy/gateway.md](deploy/gateway.md#shipping-ledgers-to-object-storage)). Once shipped, a ledger's version there cannot be deleted before its retention ends, and a later upload under the same name only adds a version beside it, so check the first version. A ledger not yet shipped is only as safe as the local copy.

## Sources

R1 to R4 were read, and the runs below made, on 2026-09-28. The Gemini CLI source read is a 0.63.0 nightly; the runs used the released 0.61.0.

| Ref | Source | Used for |
|---|---|---|
| R1 | code.claude.com/docs/en/llm-gateway ("Setting only that variable, without a gateway credential, doesn't replace the subscription … a saved claude.ai login remains the active credential"; "Gateways that pass this traffic on to Anthropic must forward the OAuth capability in `anthropic-beta`"). A run of Claude Code 2.1.284 against a recording proxy: `ANTHROPIC_CUSTOM_HEADERS` also sent on its direct `GET api.anthropic.com/api/claude_cli/bootstrap` | subscriptions through a base URL; why the run's key is in the path |
| R2 | Runs of Claude Code 2.1.284 against a recording proxy with an API key, an `apiKeyHelper` and `CLAUDE_CODE_OAUTH_TOKEN`: the headers each sends, and the hosts it reached directly. code.claude.com/docs/en/network-config (the hosts it needs). This change's end-to-end run: API key through `seatbelt run claude` | Claude Code rows |
| R3 | openai/codex at `rust-v0.158.0`, and runs of Codex 0.158.0: `requires_openai_auth` uses the stored login (`codex-rs/model-provider/src/auth.rs`); `env_http_headers` takes a header's value from an environment variable (`model-provider-info/src/lib.rs`); ChatGPT login sends `chatgpt-account-id` (`model-provider/src/bearer_auth_provider.rs`) to `chatgpt.com/backend-api/codex` (`model-provider-info/src/lib.rs`); a custom provider's `supports_websockets` defaults to false; the hosts reached directly. This change's end-to-end runs: API key and ChatGPT login through `seatbelt run codex` | Codex rows |
| R4 | google-gemini/gemini-cli at `2fe7c2d`, and runs of Gemini CLI 0.61.0: `CODE_ASSIST_ENDPOINT` sets the base of every Code Assist call (`packages/core/src/code_assist/server.ts`); the request and response wrapping (`code_assist/converter.ts`); a base URL with nothing selected picks the `gateway` mode, which is refused (`packages/core/src/core/contentGenerator.ts`, `packages/cli/src/config/auth.ts`); the hosts reached directly. This change's end-to-end run: API key through `seatbelt run gemini` | Gemini CLI rows |
