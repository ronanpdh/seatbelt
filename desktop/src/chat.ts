// A chat: the CLI's headless session through `seatbelt run`, shown as a conversation. Model
// and tool text is untrusted and only ever set as textContent. Approvals are cards the user
// answers; the CLI's own rules decide what it asks about.

import { Channel, invoke } from "@tauri-apps/api/core";
import { button, el, plain, richText } from "./dom";
import type { ChatEvent, Cli, Report, ToolStatus } from "./types";

const STATUS_TEXT: Record<ToolStatus, string> = {
  running: "running",
  done: "done",
  failed: "failed",
  declined: "declined",
};
const LOG_LINES = 500;

export type ChatHooks = {
  /** The CLI needs signing in: the session opens it in a terminal. */
  signIn: () => void;
  /** Something the sidebar shows changed. */
  changed: (state: "starting" | "running" | "ended", note: string) => void;
  ended: (report: Report | null) => void;
};

export class ChatPane {
  private id: number | null = null;
  private busy = false;
  private ended = false;
  private readonly log: HTMLPreElement;
  private readonly logBox: HTMLDetailsElement;
  private logLines = 0;
  private readonly feed: HTMLDivElement;
  private readonly input: HTMLTextAreaElement;
  private readonly sendButton: HTMLButtonElement;
  private readonly texts = new Map<string, { node: HTMLElement; text: string }>();
  private readonly tools = new Map<string, HTMLElement>();
  private readonly approvals = new Map<string, HTMLElement>();
  private signInShown = false;
  private pendingRender = new Set<string>();

  constructor(
    readonly box: HTMLElement,
    private readonly cli: Cli,
    private readonly hooks: ChatHooks,
  ) {
    box.classList.add("chat");
    this.feed = el("div", "feed");
    this.feed.setAttribute("role", "log");
    this.feed.setAttribute("aria-live", "polite");
    this.feed.append(el("p", "feed-note muted", `Each message goes to your own ${cli.label}, through seatbelt run. The session is recorded as one run.`));

    this.logBox = el("details", "log");
    this.logBox.append(el("summary", undefined, "Session log"));
    this.log = el("pre");
    this.logBox.append(this.log);

    const composer = el("form", "composer");
    this.input = el("textarea");
    this.input.rows = 3;
    this.input.placeholder = `Message ${cli.label}…  (Enter to send, Shift+Enter for a new line)`;
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        composer.requestSubmit();
      }
    });
    this.sendButton = el("button", "send", "Send");
    this.sendButton.type = "submit";
    composer.append(this.input, this.sendButton);
    composer.addEventListener("submit", (e) => {
      e.preventDefault();
      if (this.busy) void this.interrupt();
      else void this.send();
    });
    box.append(this.feed, this.logBox, composer);
  }

  async start(folder: string): Promise<void> {
    const events = new Channel<ChatEvent>();
    events.onmessage = (event) => this.handle(event);
    this.hooks.changed("starting", "starting");
    try {
      this.id = await invoke<number>("open_chat", { cli: this.cli.name, cwd: folder, events });
      if (!this.ended) this.hooks.changed("running", "recording");
    } catch (e) {
      this.say(plain(e), "error");
      this.finish(null);
    }
    this.update();
  }

  focus(): void {
    this.input.focus();
  }

  get running(): boolean {
    return this.id !== null && !this.ended;
  }

  /** End the chat: the CLI's input closes, and `seatbelt run` closes and signs the run. */
  async close(): Promise<void> {
    if (this.running) await invoke("close_chat", { id: this.id }).catch(() => undefined);
  }

  private async send(): Promise<void> {
    const text = this.input.value.trim();
    if (!text || !this.running) return;
    this.input.value = "";
    this.feed.append(el("div", "msg user", text));
    this.scroll(true);
    this.busy = true;
    this.update();
    try {
      await invoke("chat_send", { id: this.id, text });
    } catch (e) {
      this.say(plain(e), "error");
      this.busy = false;
      this.update();
    }
  }

  private async interrupt(): Promise<void> {
    if (this.running) await invoke("chat_interrupt", { id: this.id }).catch(() => undefined);
  }

  private update(): void {
    this.sendButton.textContent = this.busy ? "Stop" : "Send";
    this.sendButton.classList.toggle("stop", this.busy);
    this.sendButton.disabled = !this.running;
    this.input.disabled = !this.running;
  }

  private nearBottom(): boolean {
    return this.feed.scrollHeight - this.feed.scrollTop - this.feed.clientHeight < 80;
  }

  private scroll(force = false): void {
    if (force || this.nearBottom()) this.feed.scrollTop = this.feed.scrollHeight;
  }

  private say(text: string, kind: "error" | "note"): void {
    const stick = this.nearBottom();
    this.feed.append(el("div", `line ${kind}`, text));
    this.scroll(stick);
  }

  private handle(event: ChatEvent): void {
    const stick = this.nearBottom();
    switch (event.kind) {
      case "text":
      case "message": {
        let entry = this.texts.get(event.id);
        if (!entry) {
          entry = { node: el("div", "msg assistant"), text: "" };
          this.texts.set(event.id, entry);
          this.feed.append(entry.node);
        }
        entry.text = event.kind === "text" ? entry.text + event.delta : event.text;
        this.render(event.id);
        break;
      }
      case "tool":
        this.tool(event.id, event.name, event.detail, event.status);
        break;
      case "approval":
        this.approval(event.id, event.tool, event.detail);
        break;
      case "resolved": {
        const card = this.approvals.get(event.id);
        if (card && !card.classList.contains("answered")) {
          card.classList.add("answered");
          card.querySelector(".approval-buttons")?.replaceChildren(el("span", "muted", "No longer waiting"));
        }
        this.approvals.delete(event.id);
        break;
      }
      case "turn_end":
        this.busy = false;
        if (!event.ok && event.error) this.say(event.error, "error");
        this.update();
        break;
      case "sign_in":
        this.signInCard(event.reason);
        break;
      case "log":
        this.logLine(event.text);
        break;
      case "exit":
        this.finish(event.report);
        break;
    }
    this.scroll(stick);
  }

  /** Re-render a message once per frame, however many pieces arrive. */
  private render(id: string): void {
    if (this.pendingRender.has(id)) return;
    this.pendingRender.add(id);
    requestAnimationFrame(() => {
      this.pendingRender.delete(id);
      const entry = this.texts.get(id);
      if (!entry) return;
      const stick = this.nearBottom();
      entry.node.replaceChildren(richText(entry.text));
      this.scroll(stick);
    });
  }

  private tool(id: string, name: string, detail: string, status: ToolStatus): void {
    let row = this.tools.get(id);
    if (!row) {
      row = el("div", "tool");
      row.append(el("span", "tool-dot"), el("span", "tool-name"), el("code", "tool-detail"), el("span", "tool-status"));
      this.tools.set(id, row);
      this.feed.append(row);
    }
    if (name) row.querySelector(".tool-name")!.textContent = name;
    if (detail) row.querySelector(".tool-detail")!.textContent = detail;
    row.querySelector(".tool-status")!.textContent = STATUS_TEXT[status];
    row.dataset.status = status;
  }

  private approval(id: string, tool: string, detail: string): void {
    const card = el("div", "approval");
    card.append(el("div", "approval-title", `${this.cli.label} asks to use ${tool}`));
    if (detail) card.append(el("pre", "approval-detail", detail));
    const buttons = el("div", "approval-buttons");
    const answer = (allow: boolean) => {
      buttons.querySelectorAll("button").forEach((b) => (b.disabled = true));
      invoke("chat_answer", { id: this.id, request: id, allow })
        .then(() => {
          card.classList.add("answered", allow ? "allowed" : "denied");
          buttons.replaceChildren(el("span", "muted", allow ? "Allowed" : "Denied"));
        })
        .catch((e) => {
          buttons.replaceChildren(el("span", "error", plain(e)));
        });
    };
    buttons.append(
      button("Allow", "allow", () => answer(true), "Allow this once"),
      button("Deny", "deny", () => answer(false), "Deny; the CLI is told you said no"),
    );
    card.append(buttons);
    this.approvals.set(id, card);
    this.feed.append(card);
    (buttons.firstElementChild as HTMLButtonElement).focus({ preventScroll: true });
  }

  private signInCard(reason: string): void {
    if (this.signInShown) return;
    this.signInShown = true;
    const card = el("div", "signin");
    card.append(el("div", "approval-title", `${this.cli.label} needs signing in`));
    card.append(el("p", "muted", reason));
    card.append(
      el(
        "p",
        undefined,
        `Sign in with ${this.cli.label}'s own sign-in, in a terminal. When you leave it, the chat starts again.`,
      ),
    );
    card.append(button("Sign in in a terminal", "primary", () => this.hooks.signIn()));
    this.feed.append(card);
  }

  private logLine(text: string): void {
    this.logLines += 1;
    if (this.logLines > LOG_LINES) {
      const first = this.log.firstChild;
      if (first) first.remove();
    }
    this.log.append(el("div", undefined, text));
    this.logBox.querySelector("summary")!.textContent = `Session log (${this.logLines})`;
  }

  private finish(report: Report | null): void {
    if (this.ended) return;
    this.ended = true;
    this.busy = false;
    this.approvals.forEach((card) => card.querySelector(".approval-buttons")?.replaceChildren(el("span", "muted", "Session ended")));
    this.approvals.clear();
    const note = report?.recorded ? `recorded ${report.run}` : report ? "nothing recorded" : "ended";
    this.say(`Session ended: ${note}.`, "note");
    this.hooks.changed("ended", note);
    this.hooks.ended(report);
    this.update();
  }

  dispose(): void {
    this.box.remove();
  }
}
