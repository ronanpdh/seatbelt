# Seatbelt desktop

Run Claude Code, Codex and Gemini CLI in tabs of one window. Each tab runs its CLI through `seatbelt run`, so every session is recorded as a signed run, and each CLI keeps its own sign-in. The Runs list opens each run's HTML page in your browser.

Design and the checks still open: [docs/plans/2026-10-09-desktop-terminal-design.md](../docs/plans/2026-10-09-desktop-terminal-design.md).

**Status:** phase 1, a first build. It has been run on Linux with a stand-in CLI (the design's checks L1 to L5), and CI builds and tests it on Linux and macOS. It has not yet been run on macOS, its first target, or with a real CLI (design checks K2 to K8). Windows is phase 2.

## What it needs

- `seatbelt` with `seatbelt runs --json` (unreleased at the time of writing; from source: `uv tool install --from . seatbelt-ai` in the repository root).
- The CLIs you want to run, installed and on your `PATH`: `claude`, `codex`, `gemini`. Sign in to each in its tab, with its own sign-in (`/login` in Claude Code), as in any terminal. The app never sees your credentials.

The app looks for these on its own `PATH`, then on the `PATH` your login shell prints, then in `~/.local/bin`, `~/.claude/local`, and on macOS `/opt/homebrew/bin` and `/usr/local/bin`.

## How it works

- A tab is a pseudo-terminal (`portable-pty`) running `seatbelt run --exe <path to the CLI> <cli>` in the folder you chose, shown with xterm.js. The app has no recording code: `seatbelt run` records as it does in any terminal, locally or through your gateway (`~/.config/seatbelt/config.toml`).
- When the run ends, `seatbelt run` writes a report to a file the app names (`SEATBELT_RUN_REPORT`), and the tab shows the run it recorded.
- Closing a tab sends `seatbelt run` SIGTERM. It passes that on to the CLI, then closes and signs the run. A tab still running 20 seconds later is killed, and its ledger is closed by the next `seatbelt run`, as a killed run's is.
- The runs list is `seatbelt runs --json`. A run's page is opened by its id: the web view never names a file or a program.
- The folder a session starts in is typed (`~` works) or dropped on the window, and remembered. There are no native dialogs: macOS's folder picker can crash Tauri apps (tauri-apps/tauri#13047), so quitting with sessions running is asked in the window too.

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
