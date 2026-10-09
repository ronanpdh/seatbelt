# A desktop app that hosts the CLIs in its own terminal: design

**Goal:** a desktop app for macOS and Windows that opens Claude Code, Codex and Gemini CLI in tabs of a built-in terminal. Each tab launches its CLI through `seatbelt run`, so every session is recorded. Recording on the user's machine, each CLI keeps its own sign-in, subscriptions included; through an org's gateway, it uses the key the org issued, as `seatbelt run` does today. A runs list opens each run's HTML page.

This replaces the [desktop recorder design](2026-10-09-desktop-recorder-design.md), which is on hold: pointing the vendors' desktop apps at a gateway costs Claude Desktop its claude.ai sign-in.

**Status:** a design for review; phases 0 and 1 are built, and phase 3 (a chat window and run views, below) is being built, not to ship until K1 and K9 are answered. Facts come from the repository and the sources in the source map, read 2026-10-09. A second reader checked every claim against its source the same day and corrected eight; another checked phase 3's and found none wrong, five worded too loosely, now fixed. Everything under "Design (ours)" and "Rejected (ours)" is our decision. "Before building" lists what must be checked against the real CLIs.

## Why a terminal

- **A CLI given only a base URL keeps its subscription.** Claude Code with `ANTHROPIC_BASE_URL` set and no gateway credential still uses the saved claude.ai login, with its usage limits and billing. This is how `seatbelt run claude` records subscription users today.
- **Codex keeps its ChatGPT login too,** by another route: recording locally, `seatbelt run codex` gives Codex a provider of its own that uses Codex's stored login.
- **Through a gateway, neither keeps a subscription.** `seatbelt run` gives the CLI the key the org issued, and the gateway holds the provider keys.
- **So the app needs no recording code of its own.** A terminal that runs `seatbelt run <cli>` records exactly as `seatbelt run` does in any other terminal: locally, or through the org's gateway when `config.toml` names one. The app adds a window, tabs and a runs list.

## What Conductor does

Conductor, by Melty Labs, is the model the request named.

- **A Mac app,** not available for Windows or Linux. It runs Claude Code, Codex, Cursor and OpenCode in parallel.
- **It uses the sign-in already on the machine.** For Claude Code that is an API key or a Pro or Max login; for Codex, a Codex sign-in, an OpenAI subscription or an API key.
- **It bundles its own Claude Code and Codex by default,** for compatibility. A setting switches to the system's copy on the `PATH`. Its changelog mentions the Anthropic Agent SDK.
- **It has a chat composer, a terminal for ad hoc commands, and an experimental "Big Terminal Mode"** that replaces the centre panel with a full terminal, for any agent. Big Terminal Mode can also run custom presets. That its default chat is its own interface rather than the CLI's is our reading; no page says so.
- **One git worktree and branch per workspace.**
- **Environment variables can be set for its agents,** including `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL`, in its settings or per repository in `.conductor/settings.toml`.
- **It is not open source.** Its terms forbid derivative works and reverse engineering.

## What Anthropic's terms say

- **"Preinstalling or running Claude Code in your products or services"** (for example in hosted sandboxes or agent infrastructure), unless Anthropic has agreed otherwise, requires agreeing to its Commercial Terms of Service and these conditions:
  - the Claude Code binary must not be modified, and no built-in sign-in method may be removed, disabled or restricted;
  - customers "may not pay for, resell, or intermediate Claude usage on their end users' behalf": each end user authenticates with their own Anthropic API key, Claude subscription credentials or cloud-provider credential.
- **Third-party developers may not offer claude.ai login** in their own applications, or route requests through Free, Pro or Max plan credentials on behalf of their users.
- **Developers "may not collect, store, or intermediate Claude.ai credentials or session tokens".** Sign-in to a Claude account must complete through Anthropic's own flow.
- **What those credential rules do not prevent:** an end user signing in to the unmodified Claude Code binary with their own subscription, "including where a platform hosts Claude Code as described under" the product conditions above.
- **The Agent SDK's docs:** unless previously approved, third-party developers may not offer claude.ai login or rate limits in their products, including agents built on the SDK; they should use API keys.
- **Claude Code's headless mode** (`-p`) cannot run `/login`. Its bare mode never reads OAuth credentials, and is to become the default for `-p`.
- **Gateways:** Anthropic's gateway page says a gateway that passes subscription traffic on to Anthropic must forward the OAuth capability in `anthropic-beta`.

## A question for Anthropic before this ships

seatbelt's local recorder forwards each request, with the CLI's own credentials, to the provider, and never records them.
- Anthropic's gateway page describes subscription traffic passing through a gateway this way.
- Its legal page says developers may not "intermediate Claude.ai credentials or session tokens", and that customers may not "intermediate Claude usage on their end users' behalf".
- Its product conditions cover "preinstalling or running" Claude Code in a product. Not bundling Claude Code avoids preinstalling it; an app whose tabs launch the user's own copy may still count as running it.

So two questions are Anthropic's to answer, not ours:
1. Does a recorder on the user's own machine, in an app the seatbelt project distributes, count as intermediating credentials or usage?
2. Does an app that launches the user's own Claude Code count as running Claude Code in a product, so that the Commercial Terms and conditions apply?

The legal page sends questions about permitted authentication to Anthropic's sales team.

This applies to `seatbelt run claude` on a subscription today, not only to the app. The app makes the project look more like a product, so the question should be answered before the app ships. Building and testing can go ahead meanwhile.

The design keeps every condition it can:
- the user's own, unmodified CLI;
- sign-in through the CLI's own flow, in the terminal;
- no login offered by seatbelt;
- no credential stored or recorded.

## Embedding a terminal

- **xterm.js** is a terminal for the browser, used by VS Code. It connects to a process through a PTY library.
- **Tauri has no official PTY plugin.** Its closest plugin, `shell`, spawns child processes without a terminal.
- **`portable-pty`,** part of wezterm and MIT-licensed, gives one API over the system's PTY: Unix PTYs, and ConPTY on Windows.
- **A Tauri app can bundle an external binary** as a "sidecar", one file per target triple (CPU and system): a Mac app needs one for Apple silicon and one for Intel, or a universal binary.

## Design (ours)

### Shape

- **A Tauri v2 app.** A sidebar on the left holds the open tabs and the runs list; the main area is the selected tab's terminal (xterm.js).
- **A tab is a PTY running `seatbelt run <cli>`,** opened with `portable-pty` from the Rust side.
  - The user picks the CLI (Claude Code, Codex or Gemini CLI) and a folder to start in.
  - Keystrokes go from xterm.js to the PTY, output comes back, and a resize of the window resizes the PTY.
  - The command is built from a fixed list: the web view can ask for "claude in this folder", never for an arbitrary command line.
- **The CLI runs in its own interface,** its full terminal UI, exactly as in Terminal or Windows Terminal. seatbelt's badge, buckle and Claude Code status line show as they do there. The full buckle needs a terminal wider than 121 columns and taller than 34 rows; a smaller tab gets a line of belt.

### Sign-in

- **The CLI signs itself in, in the tab.** Claude Code's `/login`, `codex login`, Gemini CLI's own prompt. The browser steps those flows open are theirs.
- **The app never sees, asks for, stores or offers a credential.** A "Sign in" shortcut, if added, only opens a tab running the CLI's own sign-in command.

### The user's own tools

- **The CLIs are the user's installed copies,** found on their `PATH`. The app does not bundle Claude Code or Codex: the binary stays as the user installed and updates it, and the app does not preinstall Claude Code. Whether launching the user's copy counts as running it in a product is question 2 above.
- **seatbelt too, in v1.** The app needs `seatbelt` installed (`uv tool install seatbelt-ai`), checks it with `seatbelt version` against the minimum it needs, and says how to install or upgrade it.
- **Bundling seatbelt as a sidecar** (a frozen binary per platform) is a later choice, once a way to freeze it has been chosen and checked.
- **Finding tools when launched from the Dock or Start menu.** Whether the app then sees the same `PATH` as the user's shell, and how it finds `claude`, `codex` and `seatbelt` if not, is check K5.

### What seatbelt adds for the app (phase 0, in the Python package)

- **`seatbelt runs --json`:** each run's name, id, start time, status, model calls, ledger path and page path. The runs list reads this; it never parses ledgers itself.
- **A run report for the tab.** With `SEATBELT_RUN_REPORT=<file>` in its environment, `seatbelt run` writes `{"run", "cli", "recorded_by", "gateway", "recorded", "ledgers", "pages", "exit"}` there as it exits, mode 0600. The tab then knows which run it recorded without parsing terminal output. The variable is not passed on to the CLI.
- **Windows in CI, in phase 2.** CI runs on Ubuntu only, and the launcher's tests stand in for each CLI with a script started through its `#!` line; whether they run on Windows as they are is unchecked. Making them run there belongs with the Windows app, so it moves to phase 2.

### Runs list and pages

- **The list:** newest first, from `seatbelt runs --json`, refreshed when a tab's run ends.
- **A run opens its HTML page in the system's default browser,** not inside the app. The page is built to be opened from a file, and keeping it out of the app's web view keeps ledger text away from the app's bridge to the Rust side.

### Closing a tab and quitting

- **Closing a tab ends its CLI the way closing a terminal window does.** seatbelt already passes SIGHUP and SIGTERM on to the CLI, waits up to 10 seconds, then closes and signs the run. Whether this holds under `portable-pty` on macOS and under ConPTY on Windows is check K6.
- **Quitting the app asks first** while any tab is running, then closes each tab that way.

### Security

- **The app runs the CLIs as the user,** as a terminal does, and gives them nothing a terminal would not, beyond `seatbelt run`'s own environment.
- **Commands from the web view are few and fixed:** open a tab for a listed CLI in a folder; write to, resize or close a tab; list runs; open a page. There is no general command execution from the web view.
- **Terminal output is untrusted.** A model's output can hold escape sequences. Whether xterm.js lets output write the clipboard or open links, and how to turn those off, is check K8.
- **The desktop code is a second supply chain:** Rust crates and the web view's npm packages. It gets the same treatment as the Python package: locked dependencies, audits in CI, an SBOM and provenance on release.

### Where it lives

- **`desktop/` in this repository,** with its own CI and release workflows. App and `seatbelt` versions are checked against each other at run time, not released together.

### Phases

1. **Phase 0 (built):** `seatbelt runs --json` and `SEATBELT_RUN_REPORT`.
2. **Phase 1:** the macOS app. Tabs, PTYs, runs list, pages. It needs `seatbelt` and the CLIs installed.
3. **Phase 2:** the Windows app (ConPTY), Windows CI for `seatbelt run`, and signed installers for both.
4. **Phase 3:** a chat window over each CLI's headless mode, the terminal kept for sign-in, and views of runs, verification and usage. See "Phase 3" below.
5. **Later:** Conductor-style workspaces, a tab started in its own git worktree with a diff view; and seatbelt bundled as a sidecar.

## As built (phase 1, first build)

- **`desktop/`:** a Tauri 2 app (`tauri` 2.12.1, pinned in `Cargo.lock`), with a web view in TypeScript and Vite (`@xterm/xterm` 6.0.0 with only its fit addon), and npm versions pinned exactly. The app's own version is 0.1.0.
- **Rust side (`desktop/src-tauri/src/`):**
  - `pty.rs`: a tab is a `portable-pty` terminal running `seatbelt run --exe <the CLI's path> <cli>` in the chosen folder, with `TERM=xterm-256color`, the search path below as `PATH`, and `SEATBELT_RUN_REPORT` set to a file in the app's cache folder. Output goes to the web view over a Tauri channel, decoded as UTF-8 across reads; when the process exits, the report is read once, removed, and sent with the exit code. Closing a tab sends SIGTERM to `seatbelt run` (on Unix) and kills it if it is still running 20 seconds later; on Windows it is killed at once, until check K4 says how to ask a ConPTY child to end.
  - `tools.rs` (check K5, as built): the search path is the app's own `PATH`, then what the user's login shell (`$SHELL -l -i -c`) prints within 5 seconds, then `~/.local/bin`, `~/.claude/local`, and on macOS `/opt/homebrew/bin` and `/usr/local/bin`. `seatbelt` counts as supported when `seatbelt runs --json` works.
  - `runs.rs`: the list is `seatbelt runs --json --limit 200`. A page is opened by run id, from the Rust side, and only if it is an `.html` file directly in the runs folder seatbelt names.
  - `lib.rs`: nine commands (`status`, `check_folder`, `open_tab`, `write_tab`, `resize_tab`, `close_tab`, `list_runs`, `open_page`, `quit`). The window's capability grants `core:default` only: pages open from the Rust side, so the web view has no opener or shell permission.
  - **No native dialogs.** The first build picked folders with Tauri's dialog plugin and asked before quitting with a native message box. On the first macOS run, under `npm run tauri dev`, choosing a folder crashed the app: `+[NSOpenPanel openPanel]` returned NULL, and the binding the plugin uses panicked on the main thread. This is an open upstream issue (tauri-apps/tauri#13047), with an unmerged fix in wry (tauri-apps/wry#1716); its reported causes include a code-signature mismatch after an in-place update. So the plugin is gone:
    - the folder is typed (with `~` for the home folder) or dropped on the window, checked by `check_folder` (a full path to an existing folder), and remembered between launches; starting a session first applies a path typed but not yet applied;
    - quitting with sessions running (closing the window, or ⌘Q and the app menu's Quit, which macOS sends as an app-level exit request) is held, and the window asks; Quit closes each session as above, then exits.
- **Web view:** sessions and runs in a sidebar, one xterm.js terminal per session. Run names and other ledger text are set as `textContent` only. No xterm.js clipboard or links addon is loaded (check K8 still applies to xterm.js itself).
- **CI:** `.github/workflows/desktop.yml` builds and type-checks the web view, runs `npm audit`, and runs `cargo clippy -D warnings` and `cargo test`, on Ubuntu and macOS. Dependabot covers the app's npm and Cargo dependencies.

### Checks run on Linux (2026-10-09)

The app was run on Ubuntu 24.04 (WebKitGTK 2.52.6), on a virtual display (Xvfb, with openbox for the close request), with this branch's `seatbelt run`, a stand-in `claude` (a Python script that asks for a prompt, sends it to its base URL, prints the reply and waits for Enter) and a stand-in Anthropic API on localhost. These are Linux results; macOS, the first target, has not been run, and no real CLI was used.

| Ref | Check | Result |
|---|---|---|
| L1 | Start a Claude Code session; type a prompt; press Enter to end it | seatbelt's badge and the one-line belt drew in the tab; the prompt reached the CLI and its request was recorded; the run ended attested (`seatbelt verify`: "ok 4 events, chain intact, attested") with its page written; the tab named the run from the run report; the runs list refreshed |
| L2 | Close a session while the CLI is waiting for input | `seatbelt run` got SIGTERM, ended the CLI, and the run was closed attested, with its page |
| L3 | Kill the app with SIGTERM mid-session (check K6, Linux only) | the terminal closed and `seatbelt run` closed the run attested, with its page; no `seatbelt` process was left |
| L4 | Ask the window to close (`wmctrl -c`) with a session running; choose Quit | the confirmation said "1 session is still running…" (after a wording fix); Quit ended the session, the run was attested with its page, and the app exited |
| L5 | `cargo test`: 9 tests, two of them through a real pseudo-terminal with a stand-in `seatbelt` | the command line (`run --exe <path> claude`), a terminal on both ends, `TERM=xterm-256color`, keystrokes in, resize, SIGTERM on close, and the run report read once and removed |
| L6 | Opening a run's page | not checked: the container has no browser or `xdg-open` |
| L7 | After the dialog plugin was removed: a folder path that does not exist; a real one typed and a CLI clicked at once; a bad one typed and a CLI clicked | "~/… does not exist" under the field; the session started in the typed folder on the first click; no session started for the bad one, and the error showed |
| L8 | The in-window quit question: close the window with a session running; Escape; close again; Quit | the question showed, Escape kept the app open, Quit ended the session, the run was attested with its page and prompt, and the app exited |
| L9 | macOS, run by the maintainer with `npm run tauri dev` (first build) | the app opened; choosing a folder panicked as above. Fixed by removing the dialog plugin; not yet rerun on macOS |

## Phase 3: a chat window, and run views (design, 2026-10-09)

The maintainer asked for two changes after trying phase 1:
- **The terminal should only sign the CLI in;** the session should then be an ordinary chat window.
- **Runs, ledgers and reports should be viewable in the app,** as the CLI shows them: a run viewer, verification status, a usage report, and search and filters over runs.

They chose to build the chat now and ship it only once Anthropic has answered (K1); this phase adds the same question for OpenAI (K9). Tool approvals are asked in the chat, one card per tool use the CLI would ask about under its own permission rules.

### What each CLI offers for a chat client

**Claude Code**
- **A long-lived headless session.** `-p` takes `--input-format stream-json` and `--output-format stream-json`, and the Agent SDK starts the CLI that way. The Agent SDK's docs say its streaming input mode lets the agent "operate as a long lived process that takes in user input, handles interruptions, surfaces permission requests, and handles session management".
- **Permission prompts go to a host.** In print mode they go "to the Agent SDK host or the `--permission-prompt-tool` tool"; in a `-p` run with no host, they are denied.
- **The host protocol is only partly documented.** The docs describe `control_request` messages (`{type, request_id, request}`) and the allow or deny answer (`PermissionResult`). The `can_use_tool` request's fields, and `--permission-prompt-tool stdio` (which sends prompts over that protocol instead of to an MCP tool), are given only in the Python Agent SDK's source; the TypeScript reference names the request once, in passing.
- **Interrupting a turn:** SIGINT, or the SDK's `interrupt()`, which the SDK source sends as a control request with subtype `interrupt`.
- **Bare mode** skips hooks, skills, plugins, MCP servers and CLAUDE.md, never reads OAuth credentials, and "will become the default for `-p` in a future release". No flag or variable to opt out is documented on the pages read.

**Codex**
- **`codex exec --json`** prints JSONL events for one turn. Another turn is another process (`codex exec resume`). No approval requests are documented for it, and by default it runs in a read-only sandbox.
- **`codex app-server`** is "the interface Codex uses to power rich clients (for example, the Codex VS Code extension)": JSON-RPC 2.0 over stdio, newline-delimited.
  - A thread is started with `thread/start`; each message is a `turn/start`, interrupted with `turn/interrupt`.
  - Approvals are a "server-initiated JSON-RPC request to the client, and the client responds with a decision payload". A file change is answered `accept`, `acceptForSession`, `decline` or `cancel`; a command has those and more (policy amendments).
- **Two cautions on app-server.** The developer commands page says: "This is primarily for development and debugging and may change without notice." The app-server page says: "App-server authentication has never been permitted for commercial or hosted services." Just before that, it adds that a local or open-source application already using app-server authentication can continue to, though OpenAI recommends migrating to Sign in with ChatGPT.

**Gemini CLI**
- **`gemini --acp`** speaks the Agent Client Protocol: JSON-RPC over stdio.
- **Approvals:** its ACP session asks the client to approve a tool call, with options such as allow once and reject once (ACP's `session/request_permission`).
- **Its plain headless mode** treats a tool that would ask as denied.

### Checked against the real CLIs (2026-10-09)

Each CLI was installed from npm and driven by a short script, with a stand-in provider API on localhost that asks for one shell command and then answers in text. These did not go through `seatbelt run`, used API keys rather than any sign-in, and ran on Linux.

| Ref | CLI and mode | Result |
|---|---|---|
| P1 | Claude Code 2.1.295: `-p --input-format stream-json --output-format stream-json --verbose --permission-prompt-tool stdio` | a `system`/`init` event, then `assistant` events; before running Bash, a `control_request` with subtype `can_use_tool`, the tool's name and input, a description and `permission_suggestions`. Answering allow (with the input unchanged) ran the command; a second user message on stdin started a second turn in the same process |
| P2 | the same, answering deny with a message | the tool did not run; its `tool_result` was the message |
| P3 | the same, with a provider that answers 401 | `system` events with subtype `api_retry`, `error_status` 401 and `error` "authentication_failed", with `max_retries` 10 |
| P4 | Codex 0.162.0: `codex -c <provider> app-server` | `initialize`, then `account/read` (`account` null with an API key provider), `thread/start`, `turn/start`; the command came as `item/started` (a `commandExecution`), then an `item/commandExecution/requestApproval` request. `accept` ran it; `item/agentMessage/delta` and `turn/completed` followed. `-c` overrides before `app-server` were accepted, as `seatbelt run codex` puts them |
| P5 | Gemini CLI 0.63.0: `gemini --acp` | `initialize` listed its sign-in methods; `session/new` worked with an API key in the environment; the shell call came as a `tool_call` update, then `session/request_permission` with options `proceed_always`, `proceed_once` and `cancel` (kinds `allow_always`, `allow_once`, `reject_once`). Choosing `proceed_once` ran it; `session/prompt` returned `stopReason` "end_turn" |
| P6 | Claude Code 2.1.295, as P1, with control requests before the first message | `initialize` was answered with `models`, each with `value` (`default`, `opus`, `fable`, `sonnet`, `haiku`), `displayName`, `description`, `supportsEffort` and `supportedEffortLevels` (`low` to `max`); it named no current model. `get_settings` answered `applied.model` and `applied.effort` (`claude-sonnet-5-5` and `high` when started with `--model sonnet --effort high`). After `set_model` with `haiku` and `apply_flag_settings` with `{"effortLevel": "low"}`, the stand-in got `claude-haiku-5-5` with `output_config.effort` "low". Between turns, `set_model` with `default` and `effortLevel` null went back to `claude-opus-5-5` and "medium"; an effort level it did not know was ignored (after `max`, "bogus" left it at `max`); `set_model` with a model it did not know was answered as a success, and that name was sent to the provider |
| P7 | Codex 0.162.0, as P4 | `model/list` answered `data` with `id`, `model`, `displayName`, `description`, `hidden`, `isDefault`, `defaultReasoningEffort` and `supportedReasoningEfforts` (eight models; `gpt-6.1-sol` the default); `thread/start` answered the thread's `model`. `turn/start` with `model` "gpt-5.6-luna" and `effort` "low": the stand-in got that model with `reasoning.effort` "low", and the next turn, without them, kept both. `effort` "bogus" and `model` "gpt-no-such-model" were each sent to the provider as given |
| P8 | Gemini CLI 0.63.0, as P5 | `session/new` answered `models` (`availableModels`, each with `modelId`, `name` and sometimes `description`; `currentModelId` "auto") and `modes`. `session/set_model` with `sessionId` and `modelId` "gemini-2.5-pro" was answered `{}`, and the next prompt's request went to `models/gemini-2.5-pro`; "auto" went back. A model id it did not know was sent to the provider as given |

### Design (ours)

- **One protocol per CLI,** the one that can ask for approvals:
  - Claude Code: `-p` with stream-json in and out, `--verbose` (as every documented stream-json example has it), `--include-partial-messages` for text as it streams, and `--permission-prompt-tool stdio`;
  - Codex: `app-server`;
  - Gemini CLI: `--acp`.
- **Each chat is `seatbelt run --exe <path> <cli> -- <those arguments>`** with pipes instead of a terminal. One chat is one run, recorded exactly as a terminal session is. seatbelt prints its own lines to stderr, so stdout carries only the CLI's protocol.
- **The protocols stay on the Rust side.** It turns each into a few events for the window: text, a finished message, a tool's state, an approval request, end of turn, sign-in needed, a notice, exit. The window can only send a message, answer an approval, interrupt, or end the chat; it never writes protocol messages itself.
- **Approvals follow the CLI's own rules.** The app sets no permission mode, approval policy or sandbox. Each request becomes a card with Allow and Deny:
  - Claude Code: allow with the tool's input unchanged, or deny with a message;
  - Codex: `accept` or `decline`;
  - Gemini CLI: its allow-once or reject-once option.

  A request the app does not handle (another control request, a Codex user-input or permissions request, ACP file or terminal access, none of which the app offers) is answered with an error, so the CLI never waits on it.
- **The terminal is for signing in.** The chat says sign-in is needed when:
  - Claude Code reports `api_retry` with "authentication_failed" (the turn is then interrupted, not retried ten times);
  - Codex's `account/read` finds no account where one is required;
  - Gemini CLI cannot start a session.

  "Sign in" then runs the CLI in a terminal in the same pane; when that ends, the chat starts again as a new run. A "Terminal" button opens the CLI's own interface at any time.
- **Model text is untrusted.** It is shown as text, never HTML. Code fences become code blocks built element by element; there are no links or images.
- **Run views,** all from seatbelt's JSON, never from ledgers read by the app:
  - **Runs:** search by name, id or client; filter by state, CLI and date.
  - **A run:** what its page shows (`seatbelt reconstruct --json`): times, usage by model, tools, refusals, failures, and the events, with search and a filter by kind. Verification (`seatbelt verify --json`) shows beside it, and its page still opens in the browser.
  - **Usage:** `seatbelt report --json` for this machine's runs, by model, person and tool, with failed, open, unsigned and broken runs listed.

### Risks

- **Terms.** A chat over a subscription login is the use K1 asks about; Codex's app-server statement on authentication is K9. Nothing ships until both are answered.
- **Bare mode becoming the `-p` default** would take the subscription login away from Claude Code chats, with no documented opt-out. The chat would then say sign-in is needed, which signing in cannot fix; the terminal would still work. K11 watches for it.
- **Undocumented or experimental protocols:** Claude Code's `stdio` permission tool and Codex's app-server can change in any release. K11 rechecks them.

### As built (phase 3, first build)

- **Python package:** `seatbelt verify --json`, `seatbelt reconstruct --json` (what the run's page shows, after the same checks), `seatbelt report --json` with no runs (an empty report), and in `seatbelt runs --json` each run's client, chain and signature, from the events it already reads. The app counts `seatbelt` as supported when `verify --json` answers on a missing ledger.
- **Rust side (`desktop/src-tauri/src/chat/`):** `protocol.rs` (the events and the driver interface), one driver each in `claude.rs`, `codex.rs` and `acp.rs`, and `mod.rs`, which runs `seatbelt run` with pipes and feeds each stdout line through the driver. A driver does no I/O, so each is tested with lines the real CLIs printed in P1 to P5. Commands added: `open_chat`, `chat_send`, `chat_answer`, `chat_interrupt`, `close_chat`, `run_detail`, `verify_run`, `usage_report`.
- **Web view:** a session is a chat; "Open in terminal" and the sign-in card switch it to the CLI's own interface, and the chat starts again (a new run) when that ends.
  - **A reply** is a turn under the agent's name, in the order things happened: reasoning (folded), words as Markdown, tool steps that open to their input and output (the drivers send these as `tool_input`, `tool_output` and `thinking` events), and approvals, which fold to one line once answered.
  - **Markdown** is a small renderer in the app (`desktop/src/markdown.ts`) that builds elements and never parses HTML. A link opens in the browser through `open_link`, which accepts only `http` and `https`.
  - **Folders** are chosen with Browse… or from recent folders, as well as typed or dropped (`pick_folder`, `desktop/src-tauri/src/picker.rs`). On macOS the picker is `osascript`'s `choose folder`, in a process of its own, so the app's process never opens the NSOpenPanel that crashed it (L9); the start folder is passed as an argument, not as script. On Windows and Linux it is the dialog plugin, called from Rust only; the window's capability is still `core:default`.
  - **A session keeps its chat and its conversation.** The chat's history stays in the window while the CLI behind it changes. The drivers send the CLI's own id for the conversation (`conversation`), and take it back to continue it: Claude Code `--resume <id>`, Codex `thread/resume`, Gemini CLI ACP `session/load` (whose replayed history is not shown again). The terminal opens the same conversation in the CLI's own interface (`--resume <id>`, `codex resume <id>`). Before a chat or terminal starts, the chat's process ends, so two processes never write to one conversation. An id is passed to a CLI only if it is plain (letters, digits, `-`, `_`, starting with a letter or digit), so it can never be read as an option. If the CLI cannot continue (Claude Code ends with "No conversation found"; the others answer with an error), the chat says so and starts a new one.
  - **The terminal** sits under the session's header, which holds "Back to chat"; it is sized inside a frame so its last row is not cut off, and `seatbelt run` is started with `SEATBELT_QUIET=1`. Records: Runs (search; filters by outcome, verification, CLI and date), a run (verification, facts, usage, tools, refusals, failures, events with search and a kind filter), Usage.
  - **Model and effort** (after the maintainer asked for a way to switch models). Under the message box, a Model picker and, where the model takes one, an Effort picker, filled from the CLI's own list (P6 to P8): Claude Code's answer to `initialize`, Codex's `model/list` (its hidden models left out), Gemini CLI's `session/new` or `session/load`. A choice applies from the next message, and is refused unless it is on that list, since Codex and Gemini CLI send an id they do not know to the provider, and Claude Code ignores an effort it does not know. Claude Code is sent `set_model` and `apply_flag_settings` (`effortLevel`, null for the model's own default); Codex gets `model` and `effort` with each `turn/start`, with the model's default effort when none is chosen, rather than the one an earlier turn left; Gemini CLI gets `session/set_model`, and offers no effort. Before any choice the picker shows what the CLI uses: Codex's thread model, Gemini CLI's current model, or the model Claude Code names in `get_settings` and each `init`. The picker is fixed while a turn runs. The last choice for each CLI is remembered on the computer, and set as a chat starts, before its first message, if the CLI still offers it; if not, the chat says so and the CLI's own setting stays. A session that starts again (Continue, back from the terminal) keeps its choice. The terminal is not given it: there, the CLI's own model setting applies. `chat_choose` is added; `open_chat` takes `model` and `effort`.

### Checks run on Linux (2026-10-09)

The app was run on Ubuntu 24.04 under Xvfb, as for L1 to L9, with this branch's `seatbelt`, the real CLIs from npm (Claude Code 2.1.295, Codex 0.162.0, Gemini CLI 0.63.0) and stand-in provider APIs on localhost named in `[upstreams]`. Each CLI used an API key, not a subscription or ChatGPT sign-in.

| Ref | Check | Result |
|---|---|---|
| L10 | Claude Code chat: a message whose answer runs Bash; Allow; End | the text streamed in; a Bash line and an approval card showed; Allow ran the command (the file was written), the reply followed, and End closed the run: `seatbelt verify --json` said ok, 8 events, attested, with its page written |
| L11 | Codex chat in a folder Codex had not been told to trust | Codex ran the command in its read-only sandbox without asking, and it failed there ("Read-only file system"); app-server sent no item for it, so the chat showed only the reply. Driven directly, the same Codex sent the command's items in a trusted folder under each approval policy tried. So this is Codex's behaviour in an untrusted folder, not the app's; noted for K10 |
| L12 | Codex chat in a trusted folder; the stand-in asks for a command outside the sandbox; Allow | a Shell line and an approval card (the reason, the command, its folder); Allow ran it, and the run was recorded |
| L13 | Gemini CLI chat; Deny | the approval card showed; Deny: nothing ran, the reply followed, and the run was recorded and attested. Gemini CLI reported the denied tool as failed; the driver now shows it as declined (a unit test; not rerun in the app) |
| L14 | Runs: search, then a run | the search narrowed the list; a run showed "Verified", its facts, usage by model, tools and events, and an event opened to its full detail |
| L15 | One ledger altered by hand | its view said "Broken at event 2" and gave no other view of the run; Usage listed it as broken and not counted. The runs list showed its outcome as "ok", which led to the Verified column, now "broken" for it |
| L16 | Claude Code against a provider answering 401 | the sign-in card showed after the first retry, and the turn was interrupted rather than retried ten times; "Sign in in a terminal" opened Claude Code's own first-run screens through `seatbelt run`; "Back to chat" ended it, and a new chat started |
| L17 | Close the window with a chat running; Quit | the question showed; Quit ended the chat, its run was attested, and no `seatbelt` process was left |
| L19 | After the maintainer's review ("not very clear"): a Claude Code turn whose stand-in reply has reasoning, Markdown (headings, nested and numbered lists, a code block, a table, links, task boxes, a `<script>` tag, an image) and a Bash call; then a Codex turn | the reply showed under "Claude Code" with its reasoning folded, the Markdown rendered (the `<script>` tag as text, the image as a label), the Bash step opening to its input and output, and the answered approval folded to "Allowed Bash"; "Working" while it ran, "Waiting for your answer" while the approval waited, "Done in 9s" after. The Codex step opened to its command, folder and output |
| L20 | Browse…, a recent folder, and the terminal after the maintainer's review (a cut-off last row, two stacked bars, seatbelt's lines in the terminal) | on Linux, Browse… opened GTK's folder chooser in the current folder, and the folder chosen there became the session's; the previous folder showed as a recent one and switched back in one click. The terminal had one header with Back to chat and End, no seatbelt lines, and Codex's own interface showed its bottom lines in full. The macOS picker (`osascript`) was not run: it is built and tested only for the command it runs |
| L21 | After the maintainer's report that a chat disappeared on going to the terminal and back, or on switching: Claude Code and Codex chats, each to its own interface and back, then switching sessions and to Runs and back, then End and Continue | the history stayed through each; Claude Code's interface opened on the same conversation, and the turn taken there reached the model with the rest when the chat went on (8 messages sent); Codex's interface resumed the same thread, and its session file held the messages from before and after. Before the fix, a second process's reply was put into the first reply, as each process numbers its messages from 1; each start's ids are now kept apart. Gemini CLI's `session/load` was not run in the app: its driver is tested, `--list-sessions` listed the ACP sessions by their ids, and `--resume` with an unknown id said "Invalid session identifier" |
| L18 | `cargo test`: 32 tests | the three drivers against the lines from P1 to P5, and a chat through a stand-in `seatbelt` (arguments, a log line, an approval answered, the run report) |
| L22 | After the maintainer asked for a way to switch models: a Claude Code, a Codex and a Gemini CLI chat, each switched with the picker, then a message; then the app quit and started again, and a new Claude Code chat; a model switch, Claude Code's own interface, and back | Before a choice the picker showed `claude-opus-5-5` for Claude Code (from `get_settings`), Codex's configured GPT-5.5 with "Default (medium)", and Gemini CLI's Auto with no effort picker. Each switch reached the stand-in with the next message: `claude-fable-5-1` with effort "high" (its default), then "low"; `gpt-5.6-luna` with `reasoning.effort` "low"; `models/gemini-3.5-flash-lite`. The runs' ledgers name the model used: `claude-fable-5-1`, `gpt-5.6-luna`, and `claude-haiku-5-5` in the run after the terminal, which ended attested (the Gemini stand-in answers as `gemini-x`, and its ledger says so). After the restart, the new chat's first message went to Fable with low effort; back from the terminal, the conversation went on (5 messages sent) to `claude-haiku-5-5`, chosen just before. Stepping through the picker with the arrow keys first left a note per step; a note now changes while it is the last thing in the chat. With a model Gemini CLI does not offer put in the app's storage as the last choice, a new Gemini CLI chat said "Gemini CLI does not offer gemini-1.0-gone now, so it uses its own setting.", showed Auto, and the stand-in got no request for that model |
| L23 | `cargo test`: 49 tests | adds, for each driver: its model list read from lines in the shape P6 to P8 showed (Gemini CLI's cut from P8; Claude Code's from P6, with a model that takes no effort added; Codex's with made-up models), a choice checked against it, the requests that set it, and a choice from before set ahead of the first message, or left out with a log line when not offered; and the stand-in `seatbelt` answering `initialize` with two models and taking a `set_model` |

## Before building

| Id | Check | If it fails |
|---|---|---|
| K1 | Anthropic's answers to the two questions above: may the local recorder, distributed in an app, forward a subscription user's credentials; and does launching the user's own Claude Code make the app a product running it | the app ships for API-key and gateway users only, and says why; `seatbelt run` gets the same answer |
| K2 | macOS: `seatbelt run claude` in a `portable-pty` PTY shown in xterm.js under Tauri. The TUI draws correctly; `/login` completes; the badge, buckle and status line show; the run is recorded and its page written | fix the terminal size and `TERM` handling until it does |
| K3 | The same for Codex (ChatGPT login and API key) and Gemini CLI | the CLIs that fail are left out of v1 |
| K4 | Windows: `seatbelt run` under ConPTY. Ctrl-C reaches the CLI, not seatbelt; the run ends signed; the lock files and data folder work | phase 2 waits for the fixes in the Python package |
| K5 | How the app finds `claude`, `codex`, `gemini` and `seatbelt` when launched from the Dock or Start menu | the user sets each path in the app's settings |
| K6 | Closing a tab: which signal the PTY's process gets on each platform, and that the run ends signed | the app ends the run itself before it closes the PTY |
| K7 | A run's page opened in the default browser (Safari, Edge): it renders and loads nothing. The CSP was checked in Chromium only | as in the HTML report design: escaping is the defence that must hold |
| K8 | xterm.js and untrusted output: clipboard writes and links from escape sequences | turn them off, or ask before each one |
| K9 | OpenAI's answer: may a local app drive the user's own Codex through `app-server` with their ChatGPT login? Its page: "If you've built a local or open-source application using Codex app-server authentication, you can continue using it, though we recommend migrating to Sign in with ChatGPT…", and "App-server authentication has never been permitted for commercial or hosted services." | Codex chat ships for API-key and gateway users only, or Codex stays in the terminal |
| K10 | macOS, through `seatbelt run`, with real sign-ins: a Claude Code chat on a subscription (non-bare `-p`), Codex on a ChatGPT login, Gemini CLI on a Google login; each run recorded and signed, approvals working | the CLIs that fail keep the terminal only |
| K11 | On each new release of Claude Code and Codex: P1 to P5 again, and whether bare mode has become the `-p` default | the chat for that CLI says it needs updating and offers the terminal |

## Rejected (ours)

- **A chat UI of our own, in the first design.** Headless mode cannot run `/login`, bare mode never reads OAuth credentials and is to become the default for `-p`, and the Agent SDK's docs say third-party apps may not offer claude.ai login without approval. The maintainer has since chosen to build one over the unmodified CLIs, keep the terminal for sign-in, and ship only once K1 and K9 are answered (phase 3).
- **Driving Claude Code through the Agent SDK or an ACP adapter** (Zed's adapter is built on the Agent SDK). Either adds a dependency between the app and the CLI, and the SDK's docs bar offering claude.ai login without approval. The app speaks the CLI's own stream-json instead.
- **`codex exec --json` for Codex chats:** one process per turn, and no approval requests.
- **Bundling Claude Code and Codex,** as Conductor does by default. That is preinstalling Claude Code in a product, under the conditions above, and pins versions the user would otherwise update.
- **Recording agents inside Conductor** through its environment settings, or a Big Terminal Mode preset that runs `seatbelt run claude`. Either is possible later, but Conductor is macOS-only and closed source, and nothing documents whether a subscription login survives a custom base URL there. Through its environment settings it would also need an always-on recorder, as the on-hold design did.
- **Pointing the vendors' desktop apps at a gateway:** the [on-hold design](2026-10-09-desktop-recorder-design.md). It costs Claude Desktop its subscription sign-in.

## Not done

- OpenAI's terms on a ChatGPT login used through a local recorder: not researched.
- Signing, notarisation and updates on each platform: not researched.
- Linux.
- Workspaces and worktrees (later).
- Choosing a permission mode in the chat (Claude Code's `set_permission_mode`, Gemini CLI's `session/set_mode`, Codex's approval policy): not built; the app still sets none.

## Source map

All web pages read 2026-10-09.

| Section | Claim | Source |
|---|---|---|
| Why a terminal | `ANTHROPIC_BASE_URL` alone keeps the claude.ai login, its limits and billing | https://code.claude.com/docs/en/llm-gateway ("Subscriptions and gateways"): "Setting only that variable, without a gateway credential, doesn't replace the subscription. Requests still route through the gateway, but a saved claude.ai login remains the active credential, so its usage limits and billing apply." |
| Why a terminal | `seatbelt run claude` records subscription users this way | `docs/local-recording.md` ("Which sign-ins are recorded", R1) |
| Why a terminal | `seatbelt run codex` uses Codex's own stored login | `src/seatbelt/gateway/launcher.py` (`arguments`, `requires_openai_auth=true`); `docs/local-recording.md` (R3) |
| Why a terminal | `seatbelt run` records locally or through the gateway named in `config.toml` | `docs/local-recording.md` ("Through a gateway") |
| Conductor | made by Melty Labs, Inc. | https://www.conductor.build/terms: "by and between Melty Labs, Inc., a Delaware corporation" |
| Conductor | a Mac app; not for Windows or Linux | https://www.conductor.build/llms.txt: "Conductor is a Mac app that lets you run many coding agents in parallel on your codebase."; https://www.conductor.build/docs/installation: "Conductor is not available for Windows or Linux yet." |
| Conductor | runs Claude Code, Codex, Cursor and OpenCode | https://www.conductor.build/docs: "Conductor lets you run Claude Code, Codex, Cursor, and OpenCode in parallel." |
| Conductor | uses the sign-in on the machine: API key or Pro/Max for Claude Code | https://www.conductor.build/docs/faq: "If you're logged into Claude Code with an API key, Conductor uses that; if you're logged in with Claude Pro or Max, Conductor uses that." |
| Conductor | Codex sign-in, an OpenAI subscription or an API key | https://www.conductor.build/docs/guides/providers: "Codex can use Codex sign-in, an OpenAI subscription, or an API key." |
| Conductor | bundles its own Claude Code and Codex by default; can use the system's copy | https://www.conductor.build/docs/faq: "Conductor comes bundled with its own installation of Claude Code and Codex, so that we can ensure compatibility."; https://www.conductor.build/docs/reference/harnesses/claude-code: "Conductor can use the bundled Claude Code binary or a configured system Claude Code binary" |
| Conductor | changelog mentions the Anthropic Agent SDK | https://www.conductor.build/changelog/0.36.3-submit-a-prompt-sdk-memory-leaks: "We've also updated the Anthropic Agent SDK to 2.1.50" |
| Conductor | a chat composer; a terminal for ad hoc commands; Big Terminal Mode replaces the centre panel, runs any agent, has presets | https://www.conductor.build/docs/first-workspace: "Use the Run button for saved commands and the terminal for ad hoc commands."; https://www.conductor.build/changelog/0.48.0-big-terminal-mode: "You can now run a full terminal in the center panel with ⌘⇧T and run any agent you'd like." (and custom presets); https://www.conductor.build/docs/reference/big-terminal-mode: "Big Terminal Mode replaces the center panel with a full terminal" |
| Conductor | one worktree and branch per workspace | https://conductor.build/docs/concepts/git-worktrees: "When you create a workspace, Conductor creates a Git worktree for that workspace and checks out a branch inside it." |
| Conductor | environment variables, `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL`; per repository in `.conductor/settings.toml` (example: https://www.conductor.build/docs/reference/settings/example) | https://www.conductor.build/docs/guides/providers: "Claude Code can use Anthropic-compatible providers through environment variables. Add these values in `Settings` -> `Environment`."; https://conductor.build/docs/reference/environment-variables (`[environment_variables]`) |
| Conductor | not open source; terms forbid derivative works and reverse engineering | https://www.conductor.build/terms: "copy, modify, or create derivative works of any Conductor IP"; "reverse engineer, disassemble, decompile" |
| Anthropic's terms | preinstalling or running Claude Code in a product requires the Commercial Terms and conditions: binary unmodified, sign-in methods kept; no intermediating usage; each end user's own credentials | https://code.claude.com/docs/en/legal-and-compliance ("Can customers offer Claude Code in their products?"): "preinstalling or running Claude Code in your products or services (e.g. in hosted sandboxes or other agent infrastructure) requires agreeing to our Commercial Terms of Service"; "The Claude Code binary must not be modified."; "Customers may not pay for, resell, or intermediate Claude usage on their end users' behalf."; "Each end user must authenticate with their own Anthropic API key, Claude subscription plan credentials, or 3P inference provider credential" |
| Anthropic's terms | no claude.ai login offered by third parties; no routing through Free/Pro/Max credentials on users' behalf; no collecting, storing or intermediating credentials | same page ("Authentication and credential use"): "Anthropic does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users. Moreover, developers may not collect, store, or intermediate Claude.ai credentials or session tokens — sign-in to a Claude account must complete through Anthropic's own flow." |
| Anthropic's terms | the credential rules do not prevent signing in to the unmodified binary with one's own subscription, where a platform hosts it under the product conditions | same page: "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription, including where a platform hosts Claude Code as described under *Can customers offer Claude Code in their products?* above." |
| Anthropic's terms | questions about permitted authentication go to sales | same page: "For questions about permitted authentication methods for your use case, please contact sales" |
| Anthropic's terms | Agent SDK: no third-party claude.ai login unless approved | https://code.claude.com/docs/en/agent-sdk/overview: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK." |
| Anthropic's terms | headless mode cannot run `/login`; bare mode never reads OAuth; bare to become the `-p` default | https://code.claude.com/docs/en/headless: "Built-in commands that only run in the terminal interface, such as `/login`, aren't available."; "In bare mode, Claude Code never reads OAuth credentials or the system keychain."; bare "will become the default for `-p` in a future release" |
| Anthropic's terms | gateways passing subscription traffic must forward the OAuth capability | https://code.claude.com/docs/en/llm-gateway: "Gateways that pass this traffic on to Anthropic must forward the OAuth capability in `anthropic-beta`" |
| A question | the local recorder forwards the CLI's credentials and never records them | `src/seatbelt/gateway/local.py` (module docstring); `docs/local-recording.md` ("How it works": "Credentials are never recorded.") |
| Embedding | xterm.js: a terminal for the browser, used by VS Code; connects through a PTY library | https://github.com/xtermjs/xterm.js: "Xterm.js is a frontend component that enables applications to bring fully-featured terminals to their users in the browser. It's used by popular projects such as VS Code"; "a library like node-pty" |
| Embedding | no official Tauri PTY plugin; `shell` spawns child processes | https://github.com/tauri-apps/plugins-workspace (plugin list): `shell`, "Access the system shell. Allows you to spawn child processes" |
| Embedding | `portable-pty`: cross-platform PTY, part of wezterm, ConPTY on Windows, MIT | https://docs.rs/portable-pty/latest/portable_pty/: "This crate provides a cross platform API for working with the pseudo terminal (pty) interfaces provided by the system"; "This crate is part of wezterm."; https://github.com/wezterm/wezterm/blob/main/pty/src/lib.rs (`win::conpty::ConPtySystem`); `pty/Cargo.toml` (MIT) |
| Embedding | Tauri sidecar: an external binary per target triple | https://v2.tauri.app/develop/sidecar/: "a binary with the same name and a `-$TARGET_TRIPLE` suffix must exist on the specified path." |
| Design | CI runs on Ubuntu only | `.github/workflows/ci.yml` (`runs-on: ubuntu-latest`) |
| Design | `seatbelt run` passes SIGHUP and SIGTERM to the CLI, waits 10 seconds, then closes the run | `src/seatbelt/gateway/launcher.py` (`_spawn`, `GRACE = 10.0`, `_PASSED_ON`); `docs/local-recording.md` ("How it works") |
| Design | the run's page was CSP-checked in Chromium only | `docs/plans/2026-10-09-html-run-report-design.md` (check C1) |
| Why a terminal | through a gateway, the CLI uses the issued key, not its own login | `src/seatbelt/gateway/launcher.py` (gateway preset `ANTHROPIC_AUTH_TOKEN`; Codex `requires_openai_auth=false` unless local); `docs/local-recording.md` ("Through a gateway": "The CLI uses the gateway, with your issued key."); https://code.claude.com/docs/en/llm-gateway: a gateway credential is carried "in place of a developer's claude.ai subscription login" |
| Design | the buckle needs more than 121 columns and 34 rows; a smaller terminal gets a line of belt | `docs/local-recording.md` ("How it works") |
| Design | the launcher's tests stand in for the CLI with a `#!` script | `tests/helpers.py` (`fake_claude`) |
| Phase 3 | `-p` with `--input-format`/`--output-format stream-json` | https://code.claude.com/docs/en/cli-reference: `--input-format` "Specify input format for print mode (options: `text`, `stream-json`)"; `--output-format` "(options: `text`, `json`, `stream-json`)" |
| Phase 3 | the Agent SDK's streaming input mode: a long-lived process that surfaces permission requests | https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode (Streaming Input Mode): "allows the agent to operate as a long lived process that takes in user input, handles interruptions, surfaces permission requests, and handles session management." |
| Phase 3 | the Agent SDK starts the CLI with stream-json in and out | https://github.com/anthropics/claude-agent-sdk-python, `src/claude_agent_sdk/_internal/transport/subprocess_cli.py`: `"--output-format", "stream-json", "--verbose"`; "Always use streaming mode with stdin"; `"--input-format", "stream-json"` |
| Phase 3 | prompts go to the SDK host or the permission prompt tool; denied in `-p` with no host | https://code.claude.com/docs/en/cli-reference (`--permission-prompts`): "With the default `host`, Claude Code sends them to the Agent SDK host or the `--permission-prompt-tool` tool."; https://code.claude.com/docs/en/headless: "In a `-p` run with no host, these requests are denied either way" |
| Phase 3 | `control_request` shape and `PermissionResult` documented | https://code.claude.com/docs/en/agent-sdk/typescript: "the same `{ type: "control_request", request_id, request }` shape the session streams for permission requests while running"; `PermissionResult` (`behavior: "allow"` / `"deny"`) |
| Phase 3 | `can_use_tool`'s fields and `permission_prompt_tool_name="stdio"` only in the Python SDK source; the TS reference names `can_use_tool` once | https://github.com/anthropics/claude-agent-sdk-python, `src/claude_agent_sdk/types.py`: `subtype: Literal["can_use_tool"]`; "returns a copy with ``permission_prompt_tool_name="stdio"`` so the CLI sends permission requests over the control protocol." https://code.claude.com/docs/en/agent-sdk/typescript (`SDKPermissionDeniedMessage.agent_id`): "Mirrors the field on `can_use_tool` for host-side routing". `stdio` not found on the docs pages read |
| Phase 3 | interrupt: SIGINT or `interrupt()`; SDK sends subtype `interrupt` | https://code.claude.com/docs/en/headless: "send SIGINT, or call the Agent SDK's `interrupt()`"; Python SDK `_internal/query.py`: `{"subtype": "interrupt"}` |
| Phase 3 | bare mode: what it skips; no OAuth; to become the `-p` default; no documented opt-out | https://code.claude.com/docs/en/headless: "skipping auto-discovery of hooks, skills, custom commands, subagents, installed plugins, MCP servers, auto memory, and CLAUDE.md"; "In bare mode, Claude Code never reads OAuth credentials or the system keychain."; "will become the default for `-p` in a future release". Opt-out: none found in the CLI reference or https://code.claude.com/docs/en/env-vars |
| Phase 3 | `codex exec --json`: JSONL events; resume is a new process; read-only sandbox by default | https://learn.chatgpt.com/docs/non-interactive-mode: "When you enable `--json`, `stdout` becomes a JSON Lines (JSONL) stream"; `codex exec resume --last`; "By default, `codex exec` runs in a read-only sandbox." |
| Phase 3 | app-server: powers rich clients; JSON-RPC 2.0 over stdio JSONL; thread/turn methods; approvals and decisions | https://learn.chatgpt.com/docs/app-server: "the interface Codex uses to power rich clients (for example, the Codex VS Code extension)"; "bidirectional communication using JSON-RPC 2.0 messages"; "`stdio` (`--listen stdio://`, default): newline-delimited JSON (JSONL)"; `thread/start`, `turn/start`, `turn/interrupt`; "server-initiated JSON-RPC request to the client, and the client responds with a decision payload"; "`accept`, `acceptForSession`, `decline`, `cancel`" (file changes; commands add `acceptWithExecpolicyAmendment`, and the 0.162.0 types `applyNetworkPolicyAmendment`) |
| Phase 3 | app-server may change; its authentication: local or open-source apps may continue; never permitted for commercial or hosted services | https://learn.chatgpt.com/docs/developer-commands: "This is primarily for development and debugging and may change without notice."; https://learn.chatgpt.com/docs/app-server: "If you've built a local or open-source application using Codex app-server authentication, you can continue using it, though we recommend migrating to Sign in with ChatGPT…"; "App-server authentication has never been permitted for commercial or hosted services." |
| Phase 3 | `gemini --acp`: JSON-RPC over stdio | https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md: "It uses a JSON-RPC protocol over stdio to communicate between Gemini CLI agent and a client." |
| Phase 3 | ACP permission requests and option kinds | https://agentclientprotocol.com/protocol/tool-calls: "The Agent **MAY** request permission from the user before executing a tool call by calling the `session/request_permission` method"; `allow_once`, `allow_always`, `reject_once`, `reject_always` |
| Phase 3 | Gemini headless treats asking as deny | https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/policy-engine.md: "(In non-interactive mode, this is treated as `deny`.)" |
| Phase 3 | Zed's Claude ACP adapter uses the Agent SDK | https://github.com/zed-industries/claude-agent-acp (README): "implements an ACP agent by using the official Claude Agent SDK" |
| Phase 3 | seatbelt prints its own lines to stderr | `src/seatbelt/gateway/badge.py` (`say`, `buckle`, `unbuckle`: stderr by default) |
| Phase 3 | arguments after `--` go to the CLI; Codex `-c` overrides apply before a subcommand | `src/seatbelt/cli.py` (`run`: "Arguments after -- go to the CLI."); `src/seatbelt/gateway/launcher.py` (`arguments` docstring) |
| Phase 3 | P1 to P5 | run in this repository's development container on 2026-10-09; Codex's message types from `codex app-server generate-ts` (0.162.0) |
| Phase 3 | L10 to L18 | run in this repository's development container on 2026-10-09 |
| Phase 3 | P6 to P8 | run in this repository's development container on 2026-10-09 against stand-in providers on localhost |
| Phase 3 | Gemini CLI's `session/load` answers its models too | Gemini CLI 0.63.0's bundle, `loadSession`: its response holds `models: { availableModels, currentModelId }`, as `newSession`'s does; not probed |
| Phase 3 | the Agent SDK sends `set_model` as a control request | Python SDK `_internal/query.py` (`set_model`): `"subtype": "set_model"`, `"model": model` |
| Phase 3 | `turn/start` takes `model` and `effort` for that turn and the ones after | `codex app-server generate-ts` (0.162.0), `v2/TurnStartParams.ts`: "Override the model for this turn and subsequent turns."; "Override the reasoning effort for this turn and subsequent turns." |
| Phase 3 | `model/list`'s fields | the same, `v2/Model.ts` and `v2/ModelListResponse.ts` |
| Phase 3 | Gemini CLI answers `session/set_model` | Gemini CLI 0.63.0's bundle: `session_set_model: "session/set_model"`, handled by `unstable_setSessionModel` |
| Phase 3 | L22, L23 | run in this repository's development container on 2026-10-09 |
