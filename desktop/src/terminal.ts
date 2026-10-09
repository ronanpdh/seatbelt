// A CLI in its own interface: `seatbelt run <cli>` in a terminal (xterm.js over a PTY). The
// app uses it to sign a CLI in; a session can also be opened this way from its chat.

import { Channel, invoke } from "@tauri-apps/api/core";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { plain } from "./dom";
import type { Cli, Report } from "./types";

type TabEvent =
  | { kind: "output"; data: string }
  | { kind: "exit"; code: number | null; report: Report | null };

const dark = window.matchMedia("(prefers-color-scheme: dark)");
const themes = {
  light: { background: "#ffffff", foreground: "#1b1f24", cursor: "#1b1f24", selectionBackground: "#c8d9f5" },
  dark: { background: "#0d1117", foreground: "#e6edf3", cursor: "#e6edf3", selectionBackground: "#264f78" },
};
const termTheme = () => (dark.matches ? themes.dark : themes.light);
const panes = new Set<TermPane>();
dark.addEventListener("change", () => panes.forEach((p) => (p.term.options.theme = termTheme())));

export class TermPane {
  readonly term: Terminal;
  private readonly fitAddon = new FitAddon();
  private readonly host: HTMLDivElement;
  private id: number | null = null;
  /** Asked to end before it had started: it ends as soon as it does, or never starts. */
  private closeRequested = false;
  private resizer: ResizeObserver;
  running = false;

  constructor(
    readonly box: HTMLElement,
    private readonly cli: Cli,
    private readonly folder: string,
    private readonly onExit: (code: number | null, report: Report | null) => void,
    /** The conversation to continue in the CLI's own interface, by its own id. */
    private readonly resume: string | null = null,
  ) {
    this.term = new Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
      fontSize: 13,
      scrollback: 10000,
      theme: termTheme(),
      // a link a program prints (OSC 8) opens as the chat's links do: in the browser, from
      // the Rust side, http and https only; never in the app's own window
      linkHandler: {
        activate: (_event, uri) => void invoke("open_link", { url: uri }).catch(() => undefined),
        allowNonHttpProtocols: false,
      },
    });
    // no clipboard or links addon: output cannot write the clipboard, nor turn plain text
    // into links
    this.term.loadAddon(this.fitAddon);
    // the fit addon sizes the terminal to its parent's height, padding included: the margin
    // goes on the frame, and the terminal's own parent has none, so no row is cut off
    this.host = document.createElement("div");
    this.host.className = "term-host";
    box.append(this.host);
    this.term.open(this.host);
    this.resizer = new ResizeObserver(() => this.fit());
    this.resizer.observe(this.host);
    panes.add(this);
  }

  async start(): Promise<void> {
    if (this.closeRequested) {
      this.onExit(null, null);
      return;
    }
    const events = new Channel<TabEvent>();
    events.onmessage = (event) => {
      if (event.kind === "output") this.term.write(event.data);
      else {
        this.running = false;
        this.term.write("\r\n\x1b[2m[ended]\x1b[0m\r\n");
        this.onExit(event.code, event.report);
      }
    };
    this.term.onData((data) => {
      if (this.id !== null && this.running) invoke("write_tab", { id: this.id, data }).catch(() => undefined);
    });
    this.term.onResize(({ cols, rows }) => {
      if (this.id !== null && this.running) invoke("resize_tab", { id: this.id, cols, rows }).catch(() => undefined);
    });
    this.fit();
    try {
      this.running = true;
      this.id = await invoke<number>("open_tab", {
        cli: this.cli.name,
        cwd: this.folder,
        cols: this.term.cols,
        rows: this.term.rows,
        resume: this.resume,
        events,
      });
      if (this.closeRequested) await invoke("close_tab", { id: this.id }).catch(() => undefined);
    } catch (e) {
      this.running = false;
      this.term.write(`\r\n${plain(e)}\r\n`);
      this.onExit(null, null);
    }
  }

  fit(): void {
    if (this.host.offsetParent !== null) this.fitAddon.fit();
  }

  focus(): void {
    requestAnimationFrame(() => {
      this.fit();
      this.term.focus();
    });
  }

  /** End the CLI as closing a terminal would; its exit arrives through onExit. */
  async close(): Promise<void> {
    this.closeRequested = true;
    if (this.id !== null && this.running) await invoke("close_tab", { id: this.id }).catch(() => undefined);
  }

  dispose(): void {
    this.resizer.disconnect();
    panes.delete(this);
    this.term.dispose();
    this.box.remove();
  }
}
