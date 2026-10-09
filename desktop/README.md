# Seatbelt desktop

Chat with your own Claude Code, Codex and Gemini CLI in one window. Each chat runs its CLI through `seatbelt run`, so every session is recorded as a signed run, and each CLI keeps its own sign-in. Runs, a run's events and its verification, and usage across runs are views in the app.

Design, its sources and the checks still open: [docs/plans/2026-10-09-desktop-terminal-design.md](../docs/plans/2026-10-09-desktop-terminal-design.md).

**Not to be released yet.** A chat drives the CLI's headless mode with the user's own sign-in. Whether Anthropic and OpenAI permit that for an app like this is the design's questions K1 and K9; it ships only once they have answered.

**Status:** built and run on Linux, with the real CLIs (Claude Code 2.1.295, Codex 0.162.0, Gemini CLI 0.63.0) talking to stand-in providers on localhost (the design's checks L10 to L18). CI builds and tests it on Linux and macOS. It has not been run on macOS with real sign-ins (check K10). Windows is phase 2.

## What it needs

- `seatbelt` with `seatbelt verify --json` (unreleased at the time of writing; from source: `uv tool install --from . seatbelt-ai` in the repository root).
- The CLIs you want, installed and on your `PATH`: `claude`, `codex`, `gemini`. The app never sees your credentials.

The app looks for these on its own `PATH`, then on the `PATH` your login shell prints, then in `~/.local/bin`, `~/.claude/local`, and on macOS `/opt/homebrew/bin` and `/usr/local/bin`.

## How it works

- **A chat** is `seatbelt run --exe <path to the CLI> <cli> -- <headless arguments>` in the folder you chose, with pipes instead of a terminal:
  - Claude Code: `-p --input-format stream-json --output-format stream-json --verbose --include-partial-messages --permission-prompt-tool stdio`;
  - Codex: `app-server`;
  - Gemini CLI: `--acp`.

  The Rust side speaks each protocol and gives the window a few events: text, reasoning, tools with their input and output, approvals, end of turn, sign-in needed. The window can only send a message, answer an approval, interrupt, or end the chat.
- **A reply** is shown under the agent's name, in the order things happened: its reasoning (folded), its words as Markdown, its tool steps (each opens to what it was given and what came back), and any approval it asks for. A "Working" line shows while it runs. Markdown is built as elements, never parsed as HTML; a link opens in your browser, and only `http` and `https` links do.
- **Approvals** follow the CLI's own rules: the app sets no permission mode, approval policy or sandbox. Each request is a card with Allow and Deny.
- **Signing in:** when a CLI reports it is not signed in, the chat offers its own sign-in in a terminal (a pseudo-terminal running `seatbelt run <cli>`, shown with xterm.js). When that ends, the chat starts again. "Open in terminal" does the same at any time.
- **Ending a chat** closes its input, which ends the CLI's session; `seatbelt run` then closes and signs the run. One still running 3 seconds later gets SIGTERM (on Unix), and is killed after 20 more.
- **Runs, a run and usage** are `seatbelt runs --json`, `reconstruct --json`, `verify --json` and `report --json`. A run is named by its id: the web view never names a file or a program. A run's page still opens in your browser.
- **The folder** a session starts in is typed (`~` works) or dropped on the window, and remembered. There are no native dialogs: macOS's folder picker can crash Tauri apps (tauri-apps/tauri#13047), so quitting with sessions running is asked in the window too.

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
