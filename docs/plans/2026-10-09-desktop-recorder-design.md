# A desktop recorder for Claude Desktop, Cowork and Codex: design

**Goal:** record the AI apps people use outside a terminal, on their own machine or through their organisation's gateway, without launching each one through `seatbelt run`.
- **v1 apps:** Claude Desktop (and Cowork in it), and Codex in all its forms: the CLI, the IDE extension, and Codex mode in the ChatGPT desktop app.
- **The parts:** a recorder that is always on, and a tray app that shows what it records.

**Status:** a design for review. Facts come from the repository and the vendors' documentation in the source map, read 2026-10-09. Nothing here has been run against the apps yet: the checks under "Before building" must pass first. Everything under "Design (ours)" and "Rejected (ours)" is our decision.

## Why `seatbelt run` is not enough

`seatbelt run` starts a recorder for one CLI, points that CLI at it through its environment or command line, and stops it when the CLI exits. A desktop app is not started by seatbelt, runs for days, and reads its own settings, not a terminal's environment. Recording it needs a recorder that is already running whenever the app is, and the app's own settings pointed at it.

## What the apps allow

### Claude Desktop and Cowork

- **Settings, not environment.** The desktop app takes its gateway from its third-party inference configuration, not from `ANTHROPIC_BASE_URL` or `settings.json`.
- **Gateway mode replaces the claude.ai sign-in.** Users "have no Anthropic account and never sign in to Anthropic". Use is "token-based consumption billed by your cloud provider", and the app never sends Claude account credentials to the provider. No documentation says a Pro or Max subscription can be used through a gateway in Claude Desktop.
- **This differs from Claude Code.** Claude Code given only `ANTHROPIC_BASE_URL` keeps the saved claude.ai login, and the subscription's limits and billing still apply. That is why `seatbelt run claude` works on a subscription.
- **What gateway mode keeps and loses:**
  - It keeps Chat, Cowork, Code, Projects, MCP and Memory.
  - It loses sessions in Anthropic's cloud, sharing, mobile, claude.ai web access, and voice mode.
  - Chat-history search and the nightly summaries are not available.
  - Conversations are stored on the user's own disk.
  - Tool search is off by default on gateway deployments.
- **The settings.** The keys this design uses:
  - `inferenceProvider` (`gateway`);
  - `inferenceGatewayBaseUrl`, "Full URL of the inference gateway endpoint";
  - `inferenceGatewayApiKey`;
  - `inferenceGatewayAuthScheme` (`bearer` or `x-api-key`);
  - `inferenceCustomHeaders`.
- **How a machine without MDM gets them.**
  - Developer Mode must be on. Then Developer → Configure Third-Party Inference… → Import configuration loads a small JSON file.
  - The in-app window can also export such a file "for a device without MDM".
  - On Windows, the user can also write the settings as policy under `HKCU`.
  - Settings are applied at launch: Apply Changes, then Save & Restart. To go back, the user picks the Anthropic sign-in on the sign-in screen.
- **What is not documented.**
  - The in-app window saves its configurations in `Claude-3p/configLibrary/`: `_meta.json` plus one `<id>.json` per configuration. The format of those files is not documented.
  - An API key saved from the in-app window is stored unencrypted.
  - No page says whether `inferenceGatewayBaseUrl` may be plain `http`. For the Foundry provider, the docs allow `http` to a loopback address on the device.
- **Managed settings win.** A profile delivered by MDM lands in `/Library/Managed Preferences/` on macOS. This repository's Desktop guide covers fleet setup that way, with an issued key, OIDC or a credential helper.

### Codex

- **One file for every Codex.** The ChatGPT desktop app, the Codex CLI, the IDE extension and the SDK read the same configuration: `~/.codex/config.toml`, or `%USERPROFILE%\.codex\config.toml` on Windows. Codex's desktop app is now a mode of the ChatGPT desktop app.
- **The provider can be ours and the login stays theirs.** With `requires_openai_auth = true`, a custom provider signs in with ChatGPT or an API key.
  - Through a gateway, ChatGPT sign-in needs `/models` and `/responses` proxied to `https://chatgpt.com/backend-api/codex`. seatbelt's gateway already sends every OpenAI request that carries Codex's `chatgpt-account-id` header there, `/v1/models` and `/v1/responses` included.
  - `wire_api = "responses"` is the only value, which seatbelt serves.
  - The docs show a plain-`http` `base_url` to `localhost`.
- **A project cannot change it.** A project's `.codex/config.toml` ignores `model_provider` and `model_providers`.
- **The app may not see the shell's environment.** Desktop apps and IDE extensions "may not inherit environment variables from the shell". Values go in `~/.codex/.env`, then the app is restarted.
- **No per-app choice.** Profiles are an overlay the CLI loads with `--profile`. Nothing documents a profile selector in the app or the extension.

### ChatGPT (Chat mode)

No documented setting points ChatGPT's own chat at another base URL. Only its Codex mode reads `config.toml`. Chat mode is out of scope.

### What seatbelt already has

- **The local recorder** (inside `seatbelt run`):
  - It listens on a free port on `127.0.0.1`.
  - Each run gets a random key in the base URL's path (`/_seatbelt/<key>/<run>`), and a request without it gets 401.
  - It passes the client's own credentials through to the provider, and never records them.
  - Each run's ledger is closed when the run ends.
- **Dead runs are closed by name.** Every local run holds a lock file named after its run while it runs. At start, each run closes and signs any open ledger whose run's lock is free.
- **The org gateway** closes a session after `session_idle` seconds of quiet (default 900). It already serves Claude Desktop, which names no runs, that way.
- **Sessions are keyed** by principal, key and run name. A ledger's id is the principal, the run name (or a timestamp) and a random suffix.
- **Local recording's limits.** The signing key and the ledgers are readable by the user's own processes. So an agent running as the user can rewrite a ledger and sign it again. ADR 0006 rejected a per-machine proxy as the org's recording point for the same reason: the record sits on the employee's disk.

## Design (ours)

### Two parts, built in two phases

1. **Phase 1: `seatbelt desktop`, in the Python package.**
   - `serve`: the always-on recorder.
   - `install` and `uninstall`: start it at login, per platform, or stop doing so.
   - `hook <app>` and `unhook <app>`: point an app at seatbelt, or put the app back as it was.
   - `status`.

   This is usable as soon as it ships, by anyone with `uv tool install seatbelt-ai`.
2. **Phase 2: a tray app.** A thin client of the recorder.
   - Shows the state: recording on this machine, recording through the gateway at a given URL, or stopped.
   - Lists the hooked apps, the open sessions and the recent runs. Clicking a run opens its HTML page ([HTML report design](2026-10-09-html-run-report-design.md)).
   - Offers hook, unhook, and start at login.

   The recommendation is Tauri v2: it has a tray API, and it bundles an external binary as a "sidecar", which is how the recorder would ship inside it. That costs:
   - a Rust toolchain;
   - a frozen recorder binary for each target platform;
   - signing for each platform;
   - a second dependency tree to audit, with its own SBOM and provenance in the release workflow.

### The mode follows `config.toml`, as for `seatbelt run`

- **No gateway set: local.** The recorder runs on this machine, and the apps are pointed at it.
- **`gateway` and `key` set: through the gateway.** The apps are pointed at the org's gateway, and no recorder runs on the machine. `status` and the tray ask the gateway for its policy (`GET /seatbelt/policy`), which also confirms the gateway is up and the key is accepted.

### Codex

- **`hook codex` writes to `~/.codex/config.toml`:**
  - `model_provider = "seatbelt-desktop"`;
  - a `[model_providers.seatbelt-desktop]` table.

  The name differs from the `seatbelt` provider that `seatbelt run codex` passes on its command line, so the two never merge.
- **Local mode:**

  ```toml
  model_provider = "seatbelt-desktop"

  [model_providers.seatbelt-desktop]
  name = "seatbelt"
  base_url = "http://127.0.0.1:<port>/_seatbelt/<codex key>/codex/v1"
  requires_openai_auth = true
  wire_api = "responses"
  supports_websockets = false
  ```

  The run's key goes in the path, not in a header read from the environment, because the app may not see the environment.
- **Through a gateway:**
  - `base_url` is the gateway's URL plus `/v1`;
  - `env_key = "SEATBELT_GATEWAY_KEY"`, with `requires_openai_auth = false`;
  - the issued key goes in `~/.codex/.env`, mode 0600, where the docs say the app reads such values.
- **The rest of the file is not touched.**
  - The edit changes only those keys. A test checks that every other line comes out the same.
  - The previous `model_provider`, if any, is saved in seatbelt's state, and `unhook codex` puts it back.
  - The file is written to a temporary file and renamed into place.
  - `hook` says that open Codex apps and extensions must be restarted.
- **A provider of the user's own is left alone.** If `model_provider` already names a custom provider, `hook codex` refuses and says why. Recording through someone else's proxy is not in v1.
- **Every Codex launched outside seatbelt is recorded once hooked.** That includes a plain `codex` in a terminal. `seatbelt run codex` still uses its own provider and its own run.

### Claude Desktop and Cowork

- **Recording Claude Desktop means gateway mode, so it costs the claude.ai sign-in.** Before it hooks, `hook claude-desktop` (and the tray) says so plainly, and the user must confirm:
  - the app leaves the claude.ai account;
  - use is billed to an API key;
  - cloud sessions, sharing, mobile and history search are lost.
- **On a Pro or Max subscription, Claude Desktop is not recorded.** The tray says so. It points to `seatbelt run claude`, which records Claude Code and keeps the subscription. On Claude Enterprise, `seatbelt import compliance` records Desktop and Cowork without rerouting them.
- **Local mode:**
  - The base URL is `http://127.0.0.1:<port>/_seatbelt/<desktop key>/claude-desktop`.
  - The auth scheme is `x-api-key`.
  - The user enters their own Anthropic API key in Desktop's window.
  - seatbelt passes that key through to Anthropic, as the local recorder does for every CLI, and never records or stores it. seatbelt holds no provider key.
- **Through a gateway:** the setup in `docs/deploy/claude-desktop-gateway.md`, unchanged. An MDM profile for fleets, or for one machine an imported file with the gateway's URL and an issued key, OIDC or a credential helper.
- **How it is applied.**
  - seatbelt writes a JSON file for Import configuration, mode 0600, holding the base URL and the auth scheme.
  - It prints the steps: turn on Developer Mode, import the file, enter the key, Apply Changes, Save & Restart.
  - seatbelt does not write `configLibrary` itself, because its format is not documented.
  - `unhook claude-desktop` prints the way back: the Anthropic sign-in on the sign-in screen.
- **Managed settings are left alone.** When Desktop's settings come from MDM (macOS: `com.anthropic.claudefordesktop.plist` under `/Library/Managed Preferences/`), `hook claude-desktop` refuses: the org already decides where Desktop goes.

### The recorder

- **A fixed port.**
  - The recorder picks a free port on its first start and keeps it, because the apps' settings carry it.
  - If the port is taken at a later start, the recorder does not start, and `status` and the tray say why.
  - `seatbelt desktop rehook` picks a new port and rewrites the hooks. Claude Desktop then needs its file imported again.
- **One key per app.**
  - Each key is random and kept in seatbelt's desktop state, mode 0600.
  - `rehook <app>` replaces an app's key.
  - The ledger records which app a session came from, by its run name.
- **One recorder at a time.** A lock file in the data folder ensures that.
- **One ledger per session, named like a run.**
  - A session's run name is the app's label from the path plus a fresh suffix, e.g. `claude-desktop-1a2b3c4d`, so `seatbelt runs` and `seatbelt reconstruct <name>` work as they do for `seatbelt run`. This is a small change to `Sessions`: the label from the path becomes a prefix.
  - A session closes after `session_idle` seconds of quiet (default 900, as on the gateway), or when the recorder stops. It is then signed, gets its HTML page, and is shipped when a `[sink]` is set.
- **Locks, so other runs leave its sessions alone.**
  - While a session is open, the recorder holds `runs/.running/<run name>.lock`. Without it, a `seatbelt run` started meanwhile would take the recorder's open ledger for a dead run's and close it.
  - If the recorder is killed, its locks are released. The next recorder start, or the next `seatbelt run`, closes and signs its ledgers, as for a killed run.
- **One folder for everything.** It uses the same ledgers folder, signing key and sink as `seatbelt run`, so `runs`, `report`, `reconstruct` and `verify` see desktop sessions with no change.
- **A control API for the tray.** Routes under `/_seatbelt/control/<token>/` on the same port: status, recent runs, end a session now. The token is in a 0600 file in the data folder.

### Fail closed

A hooked app whose recorder is not running gets connection errors. It is never sent around the recorder. `status` and the tray show that the recorder is stopped. `unhook` is how a person goes back to direct use.

### What changes for security

- **The listener is always on, not only during a run.** As today, another process on the machine that reaches the port without a key gets a 401, and nothing is recorded.
- **The keys live for weeks, in files the user's processes can read.** Today each run's key exists for one run. A process that reads an app's key can add requests to that app's ledger. It cannot read ledgers through the recorder. This is within local recording's existing limits: the signing key is just as readable.
- **Local desktop recording keeps local recording's limits.**
  - A signature shows that the ledger has not changed since it was signed. It does not protect a ledger from an agent running as the user.
  - Where that matters, use gateway mode, or a `[sink]` with Object Lock.
- **seatbelt changes other apps' files only by `hook`, only its own keys, and always reversibly.** It never changes settings that MDM manages.

## Before building

Each of these is run against the real apps, and the results go in this document. A failure changes the design as noted.

| Id | Check | If it fails |
|---|---|---|
| K1 | Claude Desktop accepts `inferenceGatewayBaseUrl = http://127.0.0.1:<port>/...` | Desktop is recorded only through an org gateway (HTTPS) in v1; serving TLS on loopback would need a certificate the machine trusts, which is out of scope |
| K2 | Desktop keeps the base URL's path when it calls `/v1/messages` | the key goes in a header (`inferenceCustomHeaders`) instead, after checking which hosts Desktop sends it to |
| K3 | An imported file without `inferenceGatewayApiKey` can be applied and the key added in the window | seatbelt prints the settings to enter by hand instead of a file |
| K4 | Chat, Cowork and Code sessions in Desktop all send their model requests to the gateway URL; nothing goes to Anthropic directly; `seatbelt reconstruct` shows them | anything that goes direct is listed as not recorded, as `docs/local-recording.md` does for the CLIs |
| K5 | The Codex desktop app and the IDE extension, signed in with ChatGPT and with an API key, through the local recorder: each request is recorded and the ledger verifies | the forms that fail are not hooked in v1 |
| K6 | What the Codex app needs to pick up a changed `config.toml` | `hook` says what the check found |

## Rejected (ours)

- **Writing Claude Desktop's `configLibrary` directly.** Its format is not documented, and the app can change it in any release.
- **Intercepting TLS** to record ChatGPT's Chat mode, or Claude Desktop on a subscription. It installs a trusted root certificate on the user's machine and records traffic the app was never configured to send to seatbelt. That is a different product with a much larger attack surface.
- **Falling back to direct when the recorder is down.** Silent unrecorded use is what seatbelt exists to prevent.
- **One key for all apps.** One app's key could not be replaced without re-hooking the others, and the ledgers would not say which app a session came from.
- **Holding the user's Anthropic API key in seatbelt** for local Claude Desktop. Passing Desktop's own key through matches how the local recorder treats every CLI, and keeps one more secret off seatbelt's disk.

## Not done

- ChatGPT's Chat mode, which has no documented base URL setting.
- Claude Desktop on a Pro or Max subscription.
- Cursor and other IDEs.
- Chaining to a Codex provider the user already has.
- Writing Desktop's settings as `HKCU` policy on Windows, instead of an imported file.
- A tray written in Python, which would avoid a second toolchain. We did not evaluate a library for it; compare it with Tauri before phase 2.

## Source map

All web pages read 2026-10-09. Learn.chatgpt.com is where developers.openai.com/codex pages now redirect.

| Section | Claim | Source |
|---|---|---|
| Claude Desktop | gateway routing comes from the third-party inference config, not `ANTHROPIC_BASE_URL` or `settings.json` | https://code.claude.com/docs/en/llm-gateway-connect: "The desktop app reads gateway routing from its third-party inference configuration, not from `ANTHROPIC_BASE_URL` or `settings.json`." |
| Claude Desktop | users have no Anthropic account and never sign in to Anthropic | https://claude.com/docs/third-party/claude-desktop/data-storage: "users have no Anthropic account and never sign in to Anthropic" |
| Claude Desktop | token-based consumption billed by the provider | https://claude.com/docs/third-party/claude-desktop/feature-matrix: "Claude Desktop on 3P is token-based consumption billed by your cloud provider, with no seat licensing." |
| Claude Desktop | the app never sends Claude account credentials to the provider | https://claude.com/docs/third-party/claude-desktop/admin-console: "the app never sends Claude account credentials or tokens to your provider." |
| Claude Desktop | no page says a Pro or Max subscription works through a gateway in Desktop | searched the Claude Desktop third-party docs above; not found |
| Claude Desktop | Claude Code with only `ANTHROPIC_BASE_URL` keeps the claude.ai login and subscription billing | https://code.claude.com/docs/en/gateways: "a saved claude.ai login stays the active credential, so the subscription's usage limits and billing apply." |
| Claude Desktop | features kept and lost in gateway mode | https://claude.com/docs/third-party/claude-desktop/feature-matrix: feature table (Chat, Cowork, Code, Projects, MCP, Memory ✓; cloud sessions, sharing, mobile, claude.ai web, voice —) |
| Claude Desktop | chat-history search and nightly summaries unavailable | same page: "Chat-history search and nightly summary generation are not available in Chat on 3P." |
| Claude Desktop | conversations stored on local disk | https://claude.com/docs/third-party/claude-desktop/overview: "Local disk on the user's machine" |
| Claude Desktop | tool search off by default on gateway deployments | https://claude.com/docs/third-party/claude-desktop/gateway: "On gateway deployments, Claude Desktop turns tool search off by default" |
| Claude Desktop | the setting keys and auth schemes | https://claude.com/docs/third-party/claude-desktop/configuration: `inferenceProvider`, `inferenceGatewayBaseUrl` ("Full URL of the inference gateway endpoint."), `inferenceGatewayApiKey`, `inferenceGatewayAuthScheme` (`bearer`, `x-api-key`), `inferenceCustomHeaders` |
| Claude Desktop | Import configuration from Developer → Configure Third-Party Inference…; export for a device without MDM; Developer Mode needed | https://claude.com/docs/third-party/claude-desktop/bootstrap: "load from Developer → Configure Third-Party Inference… → Import configuration"; https://claude.com/docs/third-party/claude-desktop/in-app-configuration: "a configuration file for a device without MDM", Help → Troubleshooting → Enable Developer Mode |
| Claude Desktop | on Windows the user can write policy under `HKCU` | https://claude.com/docs/third-party/claude-desktop/configuration: "where the user can write it: the local configuration file, or Windows policy under `HKCU`" |
| Claude Desktop | applied at launch; Apply Changes, Save & Restart; back via the Anthropic sign-in | https://claude.com/docs/third-party/claude-desktop/configuration ("at launch"); https://claude.com/docs/third-party/claude-desktop/installation: "Click Apply Changes, then click Save & Restart."; "To return to standard Claude Desktop, choose the Anthropic sign-in option on the sign-in screen instead." |
| Claude Desktop | `configLibrary/` with `_meta.json` and `<id>.json`; format undocumented | https://claude.com/docs/third-party/claude-desktop/configuration: "`_meta.json` records which saved configuration is applied, and each configuration is a `<id>.json` file alongside it."; no schema found |
| Claude Desktop | an API key saved from the in-app window is not encrypted | https://claude.com/docs/third-party/claude-desktop/data-storage: does not encrypt "locally applied configuration, including an API key saved from the in-app configuration window" |
| Claude Desktop | no scheme rule stated for the gateway URL; Foundry allows `http` to loopback | https://claude.com/docs/third-party/claude-desktop/configuration: none for `inferenceGatewayBaseUrl`; Foundry: "Must be https, or http to a proxy at a loopback address on the device itself (127.0.0.1, localhost or [::1])." |
| Claude Desktop | MDM profile path; fleet setup with issued key, OIDC or credential helper | `docs/deploy/claude-desktop-gateway.md` (MDM section; "Choose how users authenticate") |
| Codex | the desktop app, CLI, IDE extension and SDK read the same configuration | https://learn.chatgpt.com/docs/amazon-bedrock: "The ChatGPT desktop app, Codex CLI, IDE extension, and SDK read the same local configuration layers." |
| Codex | the macOS app reads `~/.codex/config.toml`; Windows path | https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway: "The macOS app reads the same `~/.codex/config.toml`."; `%USERPROFILE%\.codex\config.toml` |
| Codex | Codex's desktop app is a mode of the ChatGPT app | https://learn.chatgpt.com/docs/app: "Choose ChatGPT or Codex." |
| Codex | `requires_openai_auth = true` lets a custom provider sign in with ChatGPT or an API key | https://learn.chatgpt.com/docs/auth: "Set `requires_openai_auth = true`… You can then sign in with ChatGPT or an API key." |
| Codex | ChatGPT sign-in through a gateway needs `/models` and `/responses` proxied to the ChatGPT backend | https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway: "The gateway's Codex-compatible base URL must proxy `/models` and `/responses` to `https://chatgpt.com/backend-api/codex`." |
| Codex | seatbelt sends OpenAI requests carrying `chatgpt-account-id` to `/backend-api/codex`, models and responses included | `src/seatbelt/gateway/app.py` (`_route`, `_CHATGPT_PREFIX`, `Route("/v1/models", forward)`) |
| Codex | `wire_api`: `responses` is the only value | https://learn.chatgpt.com/docs/config-file/config-reference: "`responses` is the only supported value" |
| Codex | a plain-`http` localhost `base_url` in the docs | https://learn.chatgpt.com/docs/config-file/config-advanced: `base_url = "http://localhost:11434/v1"` |
| Codex | a project's config ignores `model_provider` and `model_providers` | same page |
| Codex | apps and extensions may not inherit the shell's environment; use `~/.codex/.env` | https://learn.chatgpt.com/docs/amazon-bedrock: "Desktop apps and IDE extensions may not inherit environment variables from the shell. Put required values in `~/.codex/.env`, then restart the app or extension." |
| Codex | profiles are a CLI overlay; no app selector documented | https://learn.chatgpt.com/docs/config-file/config-advanced: "When you pass `--profile profile-name`, Codex loads `~/.codex/config.toml`, then overlays `~/.codex/profile-name.config.toml`." |
| ChatGPT | no documented base URL setting for Chat mode; only Codex mode reads the config | searched learn.chatgpt.com; https://learn.chatgpt.com/docs/developer-settings: "Codex agents in the app inherit the same configuration as the IDE extension and CLI." |
| seatbelt | local recorder: free port on 127.0.0.1, key in the path, 401 without it, credentials passed through, ledger closed at run end | `src/seatbelt/gateway/local.py` (module docstring, `local_recorder`); `docs/local-recording.md` ("How it works") |
| seatbelt | runs hold a lock named after the run; dead runs' ledgers closed when their lock is free | `src/seatbelt/gateway/local.py` (`_lock_file`, `_alive`, `close_dead_runs`); `docs/local-recording.md` ("Limits") |
| seatbelt | gateway `session_idle` default 900; Desktop sessions close by idle | `src/seatbelt/gateway/config.py` (`session_idle`); `docs/deploy/claude-desktop-gateway.md` (last section) |
| seatbelt | sessions keyed by principal, key and run; ledger id is principal, run or timestamp, and a suffix | `src/seatbelt/gateway/sessions.py` (`Sessions.get`, `_start`) |
| seatbelt | local signing key and ledgers readable by the user's processes; an agent can re-sign | `docs/local-recording.md` ("Limits") |
| seatbelt | ADR 0006 rejected a per-machine proxy: the record sits on the employee's disk | `docs/adr/0006-recording-gateway.md` (Rejected) |
| seatbelt | `seatbelt run codex` uses a provider named `seatbelt` given with `-c` | `src/seatbelt/gateway/launcher.py` (`arguments`) |
| seatbelt | `seatbelt import compliance` records Desktop and Cowork on Claude Enterprise | `README.md` ("Record for a team"); `docs/deploy/compliance-import.md` |
| Phase 2 | Tauri v2 bundles an external binary as a sidecar | https://v2.tauri.app/develop/sidecar/: "you can add the `externalBin` property to the `bundle` object in your `tauri.conf.json`."; "a binary with the same name and a `-$TARGET_TRIPLE` suffix must exist" |
| Phase 2 | Tauri v2 has a tray API | https://v2.tauri.app/learn/system-tray/: "Tauri allows you to create and customize a system tray for your application." |
