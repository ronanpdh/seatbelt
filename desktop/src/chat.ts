// A chat: the CLI's headless session through `seatbelt run`, shown as a conversation. Your
// messages are bubbles on the right; each reply is a turn under the agent's name, in the order
// things happened: its reasoning (folded), its words (Markdown), its tool steps (each opens to
// what it was given and what came back), and any approval it asks for. Model and tool text is
// untrusted: it is only ever set as text, and Markdown is built as elements, never as HTML.
// Under the message box, the model, effort and permission mode: the CLI's own lists (never a
// mode that runs every tool without asking), chosen from for the next message on.

import { Channel, invoke } from "@tauri-apps/api/core";
import { button, el, plain } from "./dom";
import { markdown } from "./markdown";
import type { ChatEvent, Choice, Cli, Mode, Model, Report, ToolStatus } from "./types";

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
  /** The CLI's own id for the conversation, to continue it later; null: a new one began. */
  conversation: (id: string | null) => void;
  /** The CLI would not continue the conversation and has ended: start a new one. */
  startOver: () => void;
  /** A model and effort were chosen: the session starts with them again, as new chats do.
   * After a refusal, the choice still in use (null: the CLI's own setting). */
  chose: (choice: Choice | null) => void;
  /** A permission mode was chosen: the same. After a refusal, the mode still in use (null:
   * one not offered, so none is remembered). */
  choseMode: (mode: string | null) => void;
};

/** What the CLI said of its models, last. */
type Models = Extract<ChatEvent, { kind: "models" }>;
/** What the CLI said of its permission modes, last. */
type Modes = Extract<ChatEvent, { kind: "modes" }>;

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
  /** This process was asked to continue a conversation, and the CLI refused. */
  private refused = false;
  private sentAny = false;
  /** Each start is a new process whose ids begin again: what it shows is kept apart. */
  private generation = 0;
  private exited: Promise<void> = Promise.resolve();
  private markExited: () => void = () => undefined;
  private readonly modelSelect: HTMLSelectElement;
  private readonly effortSelect: HTMLSelectElement;
  private readonly modeSelect: HTMLSelectElement;
  private readonly picker: HTMLElement;
  private modes: Modes | null = null;
  private wantedMode: string | null = null;
  /** A mode was refused: the one the CLI says it is still in is what to remember. */
  private refusedMode = false;
  /** The same for a model or effort. */
  private refusedModel = false;
  /** The chat was ended by the user (or the app): nothing starts again on its own. */
  private closeAsked = false;
  private models: Models | null = null;
  /** Asked for as this process started: said if the CLI does not offer it. */
  private wanted: Choice | null = null;
  private choosing = false;
  /** The last "your next messages go to" line: changed again, rather than added to, while it
   * is the last thing in the conversation. */
  private choiceNote: HTMLElement | null = null;

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
        // during a turn, Enter keeps what is typed for after it: only Stop stops a turn
        if (!this.busy) composer.requestSubmit();
      }
    });
    this.input.addEventListener("input", () => this.grow());
    this.sendButton = el("button", "send", "Send");
    this.sendButton.type = "submit";
    const hint = el("span", "composer-hint muted", "Enter to send · Shift+Enter for a new line");
    const row = el("div", "composer-row");
    row.append(this.input, this.sendButton);
    this.modelSelect = el("select", "pick-model");
    this.modelSelect.setAttribute("aria-label", "Model");
    this.effortSelect = el("select", "pick-effort");
    this.effortSelect.setAttribute("aria-label", "Reasoning effort");
    this.modeSelect = el("select", "pick-mode");
    this.modeSelect.setAttribute("aria-label", "Permission mode");
    this.modelSelect.addEventListener("change", () => void this.choose(true));
    this.effortSelect.addEventListener("change", () => void this.choose(false));
    this.modeSelect.addEventListener("change", () => void this.chooseMode());
    this.picker = el("div", "picker");
    this.picker.hidden = true;
    const label = (text: string, select: HTMLSelectElement) => {
      const l = el("label", "pick");
      l.append(el("span", "muted", text), select);
      l.hidden = true;
      return l;
    };
    this.picker.append(
      label("Model", this.modelSelect),
      label("Effort", this.effortSelect),
      label("Mode", this.modeSelect),
    );
    const foot = el("div", "composer-foot");
    foot.append(this.picker, hint);
    composer.append(row, foot);
    composer.addEventListener("submit", (e) => {
      e.preventDefault();
      if (this.busy) void this.interrupt();
      else void this.send();
    });
    box.append(this.feed, this.logBox, composer);
  }

  /** Start the CLI's session: a new conversation, or `resume`, by the CLI's own id, on the
   * model and effort `choice` and the permission mode `mode` if the chat offers them. What is
   * already in the window stays; `divider`, if given, marks where this part begins. */
  async start(
    folder: string,
    resume: string | null = null,
    divider = "",
    choice: Choice | null = null,
    mode: string | null = null,
  ): Promise<void> {
    this.id = null;
    this.generation += 1;
    this.ended = false;
    this.busy = false;
    this.refused = false;
    this.sentAny = false;
    this.signInShown = false;
    this.wanted = choice;
    this.wantedMode = mode;
    this.refusedMode = false;
    this.refusedModel = false;
    this.closeAsked = false;
    this.models = null; // a new process: its own lists, when it says
    this.modes = null;
    this.exited = new Promise((done) => (this.markExited = done));
    if (divider) this.feed.append(el("div", "divider", divider));
    const events = new Channel<ChatEvent>();
    // a process from an earlier start may still say something, its end included: not to
    // this one
    const generation = this.generation;
    events.onmessage = (event) => {
      if (generation === this.generation) this.handle(event);
    };
    this.hooks.changed("starting", "starting");
    this.update();
    try {
      this.id = await invoke<number>("open_chat", {
        cli: this.cli.name,
        cwd: folder,
        resume,
        model: choice?.model ?? null,
        effort: choice?.effort ?? null,
        mode,
        events,
      });
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
    this.closeAsked = true;
    if (this.running) await invoke("close_chat", { id: this.id }).catch(() => undefined);
  }

  /** End the chat and wait until it has, or `ms` has passed; true if it has ended. */
  async closeAndWait(ms: number): Promise<boolean> {
    if (!this.running) return true;
    await this.close();
    const ended = await Promise.race([
      this.exited.then(() => true),
      new Promise<boolean>((r) => setTimeout(() => r(false), ms)),
    ]);
    return ended;
  }

  /** A line in the conversation, from the session. */
  tell(text: string, kind: "error" | "note" = "note"): void {
    this.note(text, kind);
    this.scroll(true);
  }

  set hidden(hidden: boolean) {
    this.box.hidden = hidden;
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
    this.sentAny = true;
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
    // a choice is for the next message: not while a turn runs
    const fixed = !this.running || this.busy || this.choosing;
    this.modelSelect.disabled = fixed || !this.models;
    this.effortSelect.disabled = fixed || !this.models;
    this.modeSelect.disabled = fixed || !this.modes;
    this.picker.title = this.busy ? "Choose when this turn is done" : "";
  }

  // -- the model, effort and permission mode ----------------------------------------------

  private showPicker(): void {
    const show = (select: HTMLSelectElement, on: boolean) => ((select.parentElement as HTMLElement).hidden = !on);
    show(this.modelSelect, Boolean(this.models?.models.length));
    show(this.modeSelect, Boolean(this.modes?.modes.length));
    this.picker.hidden = !this.models?.models.length && !this.modes?.modes.length;
  }

  /** "Your next messages go to …", for a change just made: one line, changed again while it
   * is the last thing in the conversation. */
  private noteChoice(model: Model | undefined, effort: string | null, mode: Mode | undefined): void {
    const name = model?.name ?? this.models?.current ?? null;
    const effortText = model?.efforts.length ? `, with ${effort ?? "its default"} effort` : "";
    const text = name
      ? `Your next messages go to ${name}${effortText}${mode ? `, in ${mode.name} mode` : ""}.`
      : `Your next messages are in ${mode?.name ?? "the CLI's own"} mode.`;
    if (this.choiceNote && this.feed.lastElementChild === this.choiceNote) this.choiceNote.textContent = text;
    else {
      this.choiceNote = el("div", "line note", text);
      this.feed.append(this.choiceNote);
    }
  }

  private mode(id: string | null): Mode | undefined {
    return this.modes?.modes.find((x) => x.id === id);
  }

  /** The permission modes offered and the one in use, as the select shows them. */
  private showModes(m: Modes): void {
    this.modes = m;
    if (this.refusedMode) {
      this.refusedMode = false;
      this.hooks.choseMode(m.mode);
    }
    if (this.wantedMode) {
      if (m.mode !== this.wantedMode) {
        this.note(`${this.cli.label} does not offer the ${this.wantedMode} mode here, so it stays in its own.`, "note");
      }
      this.wantedMode = null;
    }
    const select = this.modeSelect;
    select.replaceChildren();
    if (m.mode === null) {
      // a mode not offered (its own setting): shown, not chosen again
      const own = el("option", undefined, m.current ? `${m.current} (current)` : "Current mode");
      own.value = "";
      own.disabled = true;
      own.title = `${this.cli.label}'s own setting`;
      select.append(own);
    }
    for (const mode of m.modes) {
      const option = el("option", undefined, mode.name);
      option.value = mode.id;
      option.title = mode.description;
      select.append(option);
    }
    select.value = m.mode ?? "";
    select.title = this.mode(m.mode)?.description ?? "";
    this.showPicker();
    this.update();
  }

  /** The user chose a permission mode: it applies from the next message. */
  private async chooseMode(): Promise<void> {
    const m = this.modes;
    if (!m || !this.running || this.busy) return;
    const mode = this.mode(this.modeSelect.value);
    if (!mode) return;
    this.choosing = true;
    this.update();
    try {
      await invoke("chat_mode", { id: this.id, mode: mode.id });
      this.noteChoice(this.model(this.models?.model ?? null), this.models?.effort ?? null, mode);
      this.hooks.choseMode(mode.id);
    } catch (e) {
      this.note(plain(e), "error");
      this.showModes(m); // back to what is in use
    } finally {
      this.choosing = false;
      this.update();
      this.scroll(true);
    }
  }

  /** The CLI's models and what is in use, as the selects show them. */
  private showModels(m: Models): void {
    this.models = m;
    if (this.refusedModel) {
      this.refusedModel = false;
      this.hooks.chose(m.model ? { model: m.model, effort: m.effort } : null);
    }
    if (this.wanted) {
      if (m.model !== this.wanted.model) {
        const was = this.wanted.effort ? `${this.wanted.model} with ${this.wanted.effort} effort` : this.wanted.model;
        this.note(`${this.cli.label} does not offer ${was} now, so it uses its own setting.`, "note");
      }
      this.wanted = null;
    }
    const models = this.modelSelect;
    models.replaceChildren();
    if (m.model === null) {
      // the CLI's own setting, which is not one of its list: shown, not chosen again
      const own = el("option", undefined, m.current ? `${m.current} (current)` : "Current setting");
      own.value = "";
      own.disabled = true;
      own.title = `${this.cli.label}'s own setting`;
      models.append(own);
    }
    for (const model of m.models) {
      const option = el("option", undefined, model.name);
      option.value = model.id;
      option.title = model.description;
      models.append(option);
    }
    models.value = m.model ?? "";
    const chosen = this.model(m.model);
    models.title = chosen?.description ?? "";
    this.showEfforts(chosen, m.effort);
    this.showPicker();
    this.update();
  }

  private model(id: string | null): Model | undefined {
    return this.models?.models.find((x) => x.id === id);
  }

  private showEfforts(model: Model | undefined, effort: string | null): void {
    const select = this.effortSelect;
    select.replaceChildren();
    const label = select.parentElement as HTMLElement;
    label.hidden = !model || model.efforts.length === 0;
    if (label.hidden || !model) return;
    const own = el("option", undefined, model.default_effort ? `Default (${model.default_effort})` : "Default");
    own.value = "";
    select.append(own);
    for (const e of model.efforts) {
      const option = el("option", undefined, e);
      option.value = e;
      select.append(option);
    }
    select.value = effort && model.efforts.includes(effort) ? effort : "";
  }

  /** The user chose a model or an effort: it applies from the next message. */
  private async choose(modelChanged: boolean): Promise<void> {
    const m = this.models;
    if (!m || !this.running || this.busy) return;
    const model = this.model(this.modelSelect.value);
    if (!model) return;
    // a new model keeps the effort if it takes it; else it is the model's own default
    const kept = m.effort !== null && model.efforts.includes(m.effort) ? m.effort : null;
    const effort = modelChanged ? kept : this.effortSelect.value || null;
    const choice: Choice = { model: model.id, effort };
    this.choosing = true;
    this.update();
    try {
      await invoke("chat_choose", { id: this.id, model: choice.model, effort: choice.effort });
      this.noteChoice(model, effort, this.mode(this.modes?.mode ?? null));
      this.hooks.chose(choice);
    } catch (e) {
      this.note(plain(e), "error");
      this.showModels(m); // back to what is in use
    } finally {
      this.choosing = false;
      this.update();
      this.scroll(true);
    }
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
    const key = (id: string) => `${this.generation}:${id}`;
    switch (event.kind) {
      case "text":
        this.write(key(event.id), event.delta, false, false);
        break;
      case "message":
        this.write(key(event.id), event.text, true, false);
        break;
      case "thinking":
        this.write(key(event.id), event.delta, false, true);
        break;
      case "tool":
        this.step(key(event.id), event.name, event.detail, event.status);
        break;
      case "tool_input":
        this.stepPart(key(event.id), "Input", event.input);
        break;
      case "tool_output":
        this.stepPart(key(event.id), "Output", event.output || "(no output)");
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
      case "conversation":
        this.hooks.conversation(event.id);
        break;
      case "resumed":
        if (!event.ok) {
          this.refused = true;
          this.note(`${this.cli.label} could not continue the conversation, so this is a new one.`, "note");
          this.hooks.conversation(null);
        }
        break;
      case "models":
        this.showModels(event);
        break;
      case "modes":
        this.showModes(event);
        break;
      case "notice":
        // a change refused: the line that announced it, if nothing came after, goes, and what
        // is remembered for the next chat is what is still in use
        if (this.choiceNote && this.feed.lastElementChild === this.choiceNote) this.choiceNote.remove();
        this.choiceNote = null;
        if (event.refused === "mode") this.refusedMode = true;
        else this.refusedModel = true;
        this.note(event.text, "error");
        break;
      case "note":
        this.note(event.text, "note");
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
    // the card does not take the keyboard: a key meant for the message box must not answer it
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
    this.markExited();
    if (this.refused && !this.sentAny && !this.closeAsked && report?.recorded !== true) {
      // Claude Code ends when it cannot resume: start again, on a new conversation (not when
      // the chat was ended: Codex and Gemini CLI go on in a new one, and end when asked)
      this.hooks.startOver();
      return;
    }
    this.approvals.forEach((card) =>
      card.querySelector(".approval-buttons")?.replaceChildren(el("span", "muted", "Session ended")),
    );
    this.approvals.clear();
    if (this.busy) this.endTurn(false, "Session ended");
    this.closeReply();
    this.busy = false;
    const note = report?.recorded ? `recorded ${report.run}` : report ? "nothing recorded" : "ended";
    this.note(`Chat closed: ${note}.`, "note");
    this.hooks.changed("ended", note);
    this.hooks.ended(report);
    this.update();
  }
}
