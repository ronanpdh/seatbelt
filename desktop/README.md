# Seatbelt desktop

Chat with your own Claude Code, Codex and Gemini CLI in one window. Each chat runs its CLI through `seatbelt run`, so every session is recorded as a signed run, and each CLI keeps its own sign-in. Runs, a run's events and its verification, and usage across runs are views in the app.

Design, its sources and the checks still open: [docs/plans/2026-10-09-desktop-terminal-design.md](../docs/plans/2026-10-09-desktop-terminal-design.md).

**Not to be released yet.** A chat drives the CLI's headless mode with the user's own sign-in. Whether Anthropic and OpenAI permit that for an app like this is the design's questions K1 and K9; it ships only once they have answered.

**Status:** built and run on Linux, with the real CLIs (Claude Code 2.1.295, Codex 0.162.0, Gemini CLI 0.63.0) talking to stand-in providers on localhost (the design's checks L10 to L25). CI builds and tests it on Linux and macOS. It has not been run on macOS with real sign-ins (check K10). Windows is phase 2.

## What it needs

- `seatbelt` with `seatbelt verify --json` (unreleased at the time of writing; from source: `uv tool install --from . seatbelt-ai` in the repository root).
- The CLIs you want, installed and on your `PATH`: `claude`, `codex`, `gemini`. The app never sees your credentials.

The app looks for these on its own `PATH`, then on the `PATH` your login shell prints, then in `~/.local/bin`, `~/.claude/local`, and on macOS `/opt/homebrew/bin` and `/usr/local/bin`.

## How it works

- **A chat** is `seatbelt run --exe <path to the CLI> <cli> -- <headless arguments>` in the folder you chose, with pipes instead of a terminal:
  - Claude Code: `-p --input-format stream-json --output-format stream-json --verbose --include-partial-messages --permission-prompt-tool stdio`;
  - Codex: `app-server`;
  - Gemini CLI: `--acp`.

  The Rust side speaks each protocol and gives the window a few events: text, reasoning, tools with their input and output, approvals, end of turn, sign-in needed. The window can only send a message, answer an approval, choose a model or a permission mode the chat offers, interrupt, or end the chat.
- **A reply** is shown under the agent's name, in the order things happened: its reasoning (folded), its words as Markdown, its tool steps (each opens to what it was given and what came back), and any approval it asks for. A "Working" line shows while it runs. Markdown is built as elements, never parsed as HTML; a link opens in your browser, and only `http` and `https` links do.
- **Model and effort** are picked under the message box, from the CLI's own list: Claude Code's answer to `initialize`, Codex's `model/list`, Gemini CLI's session (which offers no effort). A choice applies from the next message, and only an id on that list is sent. The last choice for each CLI is remembered on this computer and set before a new chat's first message, if the CLI still offers it; otherwise the CLI's own setting stays, and the chat says so.
- **Permission mode** is picked beside the model, from every mode but those that run every tool without asking: Claude Code's Manual, Accept edits, Plan, Auto and Don't ask; Gemini CLI's Default, Auto Edit and Plan; Codex's Auto and Read-only presets. A mode that runs every tool without asking (Claude Code's `bypassPermissions`, Gemini CLI's YOLO, Codex's full access) is never offered or set. The picker follows the CLI when it changes mode itself, as when a plan is approved; a mode the CLI refuses (Gemini CLI refuses Auto Edit in a folder it does not trust) is put back, and the chat says why. The last mode for each CLI is remembered, as the model is.
- **Approvals** follow the CLI's own rules in the mode it is in: the app sets no permission mode, approval policy or sandbox unless you pick one. Each request is a card with Allow and Deny.
- **Signing in:** when a CLI reports it is not signed in, the chat offers its own sign-in in a terminal (a pseudo-terminal running `seatbelt run <cli>`, shown with xterm.js). When that ends, the chat starts again. "Open in terminal" does the same at any time.
- **Ending a chat** closes its input, which ends the CLI's session; `seatbelt run` then closes and signs the run. One still running 3 seconds later gets SIGTERM (on Unix), and is killed after 20 more.
- **Runs, a run and usage** are `seatbelt runs --json`, `reconstruct --json`, `verify --json` and `report --json`. A run is named by its id: the web view never names a file or a program. A run's page still opens in your browser.
- **The folder** a session starts in is chosen with Browse… (the system's folder picker), picked from your recent folders, typed (`~` works) or dropped on the window. On macOS the picker is AppKit's `choose folder`, run by `osascript` in a process of its own: in the app's own process, macOS's folder panel can come back empty and crash Tauri apps (tauri-apps/tauri#13047). On Windows and Linux it is Tauri's dialog plugin, called from the Rust side only. Quitting with sessions running is asked in the window.
- **A session keeps its chat** while you switch to another session, to Runs or Usage, or to the CLI's own interface and back. It keeps its conversation too: the CLI's own id for it (Claude Code's session, Codex's thread, Gemini CLI's ACP session) is passed back when the chat starts again (`--resume`, `thread/resume`, `session/load`), and when the terminal opens (`--resume <id>`, or `codex resume <id>`), so the agent remembers what was said on either side. If a CLI cannot continue it, the chat says so and starts a new one. Ids are checked to be plain (letters, digits, `-` and `_`) before any is passed to a CLI. Each start is a new run.
- **The terminal** shows the CLI's own interface under the session's header, with "Back to chat" there. `seatbelt run` is started with `SEATBELT_QUIET=1`, so its belt and its own lines stay out of it; its warnings still show.

Everything that came from a ledger, a model or a tool is shown as text, never as HTML.

## Develop

Needs Node 22 and Rust (stable), and on Linux Tauri's system libraries (`libwebkit2gtk-4.1-dev`, `libxdo-dev`, `libssl-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`).

```sh
cd desktop
npm ci
npm run tauri dev          # the app, reloading the web view as you edit
npm run build              # type-check and build the web view into dist/
cd src-tauri
cargo test --locked        # the Rust tests (they need dist/ built first)
cargo clippy --locked --all-targets -- -D warnings
```
