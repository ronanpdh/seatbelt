// A chat: the CLI's headless session through `seatbelt run`, shown as a conversation. Your
// messages are bubbles on the right; each reply is a turn under the agent's name, in the order
// things happened: its reasoning (folded), its words (Markdown), its tool steps (each opens to
// what it was given and what came back), and any approval it asks for. Model and tool text is
// untrusted: it is only ever set as text, and Markdown is built as elements, never as HTML.

import { Channel, invoke } from "@tauri-apps/api/core";
import { button, el, plain } from "./dom";
import { markdown } from "./markdown";
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

/** One reply: the agent's part of a turn. */
type Reply = {
  box: HTMLElement;
  body: HTMLElement;
  working: HTMLElement;
  workingText: HTMLElement;
  started: number;
};

type Prose = { node: HTMLElement; text: string; thinking: boolean };

function seconds(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

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
  private reply: Reply | null = null;
  private readonly prose = new Map<string, Prose>();
  private readonly steps = new Map<string, HTMLDetailsElement>();
  private readonly approvals = new Map<string, HTMLElement>();
  private readonly pendingRender = new Set<string>();
  private signInShown = false;
  private ticker: number | null = null;

  constructor(
    readonly box: HTMLElement,
    private readonly cli: Cli,
    private readonly hooks: ChatHooks,
  ) {
    box.classList.add("chat");
    this.feed = el("div", "feed");
    this.feed.setAttribute("role", "log");
    this.feed.setAttribute("aria-live", "polite");
    const intro = el("div", "feed-intro");
    intro.append(
      el("div", "intro-title", `Chat with ${cli.label}`),
      el("p", "muted", `Your own ${cli.label}, through seatbelt run: the whole chat is recorded as one signed run.`),
    );
    this.feed.append(intro);

    this.logBox = el("details", "log");
    this.logBox.append(el("summary", undefined, "Session log"));
    this.log = el("pre");
    this.logBox.append(this.log);

    const composer = el("form", "composer");
    this.input = el("textarea");
    this.input.rows = 2;
    this.input.placeholder = `Message ${cli.label}`;
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        composer.requestSubmit();
      }
    });
    this.input.addEventListener("input", () => this.grow());
    this.sendButton = el("button", "send", "Send");
    this.sendButton.type = "submit";
    const hint = el("div", "composer-hint muted", "Enter to send · Shift+Enter for a new line");
    const row = el("div", "composer-row");
    row.append(this.input, this.sendButton);
    composer.append(row, hint);
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
      this.note(plain(e), "error");
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

  dispose(): void {
    this.stopTicker();
    this.box.remove();
  }

  // -- sending ----------------------------------------------------------------------------

  private async send(): Promise<void> {
    const text = this.input.value.trim();
    if (!text || !this.running) return;
    this.input.value = "";
    this.grow();
    this.closeReply();
    const turn = el("div", "turn-user");
    turn.append(el("div", "msg-user", text));
    this.feed.append(turn);
    this.openReply();
    this.busy = true;
    this.update();
    this.scroll(true);
    try {
      await invoke("chat_send", { id: this.id, text });
    } catch (e) {
      this.say(plain(e), "error");
      this.endTurn(false, null);
    }
  }

  private async interrupt(): Promise<void> {
    if (this.running) await invoke("chat_interrupt", { id: this.id }).catch(() => undefined);
  }

  private grow(): void {
    this.input.style.height = "auto";
    this.input.style.height = `${Math.min(this.input.scrollHeight, window.innerHeight * 0.4)}px`;
  }

  private update(): void {
    this.sendButton.textContent = this.busy ? "Stop" : "Send";
    this.sendButton.classList.toggle("stop", this.busy);
    this.sendButton.title = this.busy ? "Stop this turn" : "Send (Enter)";
    this.sendButton.disabled = !this.running;
    this.input.disabled = !this.running;
  }

  // -- the reply in progress --------------------------------------------------------------

  private openReply(): Reply {
    const box = el("div", "reply");
    const head = el("div", "reply-head");
    head.append(el("span", `agent-mark ${this.cli.name}`), el("span", "agent-name", this.cli.label));
    const body = el("div", "reply-body");
    const working = el("div", "working");
    const workingText = el("span", undefined, "Working");
    const dots = el("span", "dots");
    dots.append(el("i"), el("i"), el("i"));
    working.append(dots, workingText);
    body.append(working);
    box.append(head, body);
    this.feed.append(box);
    this.reply = { box, body, working, workingText, started: Date.now() };
    this.startTicker();
    return this.reply;
  }

  /** The reply things go into: the one in progress, or one for things that came unasked. */
  private current(): Reply {
    return this.reply ?? this.openReply();
  }

  /** Add to the reply, keeping its working line last. */
  private add(node: HTMLElement): void {
    const r = this.current();
    r.body.insertBefore(node, r.working);
  }

  private closeReply(): void {
    if (!this.reply) return;
    this.reply.working.remove();
    if (!this.reply.body.childElementCount) this.reply.box.remove(); // nothing came
    this.reply = null;
    this.stopTicker();
  }

  private startTicker(): void {
    this.stopTicker();
    this.ticker = window.setInterval(() => this.tick(), 1000);
    this.tick();
  }

  private stopTicker(): void {
    if (this.ticker !== null) window.clearInterval(this.ticker);
    this.ticker = null;
  }

  private tick(): void {
    const r = this.reply;
    if (!r) return;
    const waiting = this.approvals.size > 0;
    r.working.hidden = !this.busy;
    r.working.classList.toggle("waiting", waiting);
    r.workingText.textContent = `${waiting ? "Waiting for your answer" : "Working"} · ${seconds(Date.now() - r.started)}`;
  }

  private endTurn(ok: boolean, error: string | null): void {
    this.busy = false;
    const r = this.reply;
    if (r) {
      // reasoning that was still streaming is finished now
      r.body.querySelectorAll("details.thinking.live").forEach((d) => {
        d.classList.remove("live");
        d.querySelector("summary")!.textContent = "Thought";
      });
      const foot = el("div", `reply-foot${ok ? "" : " failed"}`);
      foot.textContent = ok ? `Done in ${seconds(Date.now() - r.started)}` : error ? error : "Stopped";
      r.body.insertBefore(foot, r.working);
    }
    this.closeReply();
    this.update();
  }

  // -- events -----------------------------------------------------------------------------

  private nearBottom(): boolean {
    return this.feed.scrollHeight - this.feed.scrollTop - this.feed.clientHeight < 80;
  }

  private scroll(force = false): void {
    if (force || this.nearBottom()) this.feed.scrollTop = this.feed.scrollHeight;
  }

  /** A line in the reply, for errors that belong to it. */
  private say(text: string, kind: "error" | "note"): void {
    this.add(el("div", `line ${kind}`, text));
  }

  /** A line in the conversation, outside any reply. */
  private note(text: string, kind: "error" | "note"): void {
    this.feed.append(el("div", `line ${kind}`, text));
  }

  private handle(event: ChatEvent): void {
    const stick = this.nearBottom();
    switch (event.kind) {
      case "text":
        this.write(event.id, event.delta, false, false);
        break;
      case "message":
        this.write(event.id, event.text, true, false);
        break;
      case "thinking":
        this.write(event.id, event.delta, false, true);
        break;
      case "tool":
        this.step(event.id, event.name, event.detail, event.status);
        break;
      case "tool_input":
        this.stepPart(event.id, "Input", event.input);
        break;
      case "tool_output":
        this.stepPart(event.id, "Output", event.output || "(no output)");
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
        this.tick();
        break;
      }
      case "turn_end":
        this.endTurn(event.ok, event.ok ? null : event.error);
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

  /** Words or reasoning: created by the first piece, rendered once a frame. */
  private write(id: string, text: string, whole: boolean, thinking: boolean): void {
    let p = this.prose.get(id);
    if (!p) {
      if (thinking) {
        const box = el("details", "thinking live");
        box.append(el("summary", undefined, "Thinking…"));
        const content = el("div", "md thinking-body");
        box.append(content);
        this.add(box);
        p = { node: content, text: "", thinking };
      } else {
        const node = el("div", "md");
        this.add(node);
        p = { node, text: "", thinking };
      }
      this.prose.set(id, p);
    }
    p.text = whole ? text : p.text + text;
    this.render(id);
  }

  private render(id: string): void {
    if (this.pendingRender.has(id)) return;
    this.pendingRender.add(id);
    requestAnimationFrame(() => {
      this.pendingRender.delete(id);
      const p = this.prose.get(id);
      if (!p) return;
      const stick = this.nearBottom();
      p.node.replaceChildren(markdown(p.text));
      this.scroll(stick);
    });
  }

  /** A tool step: consecutive steps share one group. */
  private step(id: string, name: string, detail: string, status: ToolStatus): void {
    let step = this.steps.get(id);
    if (!step) {
      step = el("details", "step");
      const summary = el("summary");
      summary.append(el("span", "step-dot"), el("span", "step-name"), el("span", "step-detail"), el("span", "step-status"));
      step.append(summary, el("div", "step-body"));
      const r = this.current();
      const last = r.working.previousElementSibling;
      let group = last?.classList.contains("steps") ? (last as HTMLElement) : null;
      if (!group) {
        group = el("div", "steps");
        this.add(group);
      }
      group.append(step);
      this.steps.set(id, step);
    }
    if (name) step.querySelector(".step-name")!.textContent = name;
    if (detail) {
      step.querySelector(".step-detail")!.textContent = detail.split("\n")[0];
      step.title = detail;
    }
    step.querySelector(".step-status")!.textContent = STATUS_TEXT[status];
    step.dataset.status = status;
  }

  private stepPart(id: string, label: "Input" | "Output", text: string): void {
    const step = this.steps.get(id);
    if (!step) return;
    const body = step.querySelector(".step-body")!;
    let part = body.querySelector<HTMLElement>(`.part-${label.toLowerCase()}`);
    if (!part) {
      part = el("div", `part part-${label.toLowerCase()}`);
      part.append(el("div", "part-label", label), el("pre"));
      // the output goes after the input
      if (label === "Input") body.prepend(part);
      else body.append(part);
    }
    part.querySelector("pre")!.textContent = text;
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
          // answered: one line, which opens to what was asked
          card.classList.add("answered", allow ? "allowed" : "denied");
          const done = el("details", "approval-done");
          const summary = el("summary");
          summary.append(el("strong", undefined, allow ? "Allowed" : "Denied"), document.createTextNode(` ${tool}`));
          done.append(summary);
          if (detail) done.append(el("pre", "approval-detail", detail));
          card.replaceChildren(done);
          this.tick();
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
    this.add(card);
    this.tick();
    (buttons.firstElementChild as HTMLButtonElement).focus({ preventScroll: true });
  }

  private signInCard(reason: string): void {
    if (this.signInShown) return;
    this.signInShown = true;
    const card = el("div", "signin");
    card.append(el("div", "approval-title", `${this.cli.label} needs signing in`));
    card.append(el("p", "muted", reason));
    card.append(
      el("p", undefined, `Sign in with ${this.cli.label}'s own sign-in, in a terminal. When you leave it, the chat starts again.`),
    );
    card.append(button("Sign in in a terminal", "primary", () => this.hooks.signIn()));
    this.add(card);
  }

  private logLine(text: string): void {
    this.logLines += 1;
    if (this.logLines > LOG_LINES) this.log.firstChild?.remove();
    this.log.append(el("div", undefined, text));
    this.logBox.querySelector("summary")!.textContent = `Session log (${this.logLines})`;
  }

  private finish(report: Report | null): void {
    if (this.ended) return;
    this.ended = true;
    this.approvals.forEach((card) =>
      card.querySelector(".approval-buttons")?.replaceChildren(el("span", "muted", "Session ended")),
    );
    this.approvals.clear();
    if (this.busy) this.endTurn(false, "Session ended");
    this.closeReply();
    this.busy = false;
    const note = report?.recorded ? `recorded ${report.run}` : report ? "nothing recorded" : "ended";
    this.note(`Session ended: ${note}.`, "note");
    this.hooks.changed("ended", note);
    this.hooks.ended(report);
    this.update();
  }
}
