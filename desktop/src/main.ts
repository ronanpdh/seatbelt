// The Seatbelt window. A session is a chat with one of the CLIs, run through `seatbelt run`
// so it is recorded; the CLI's own terminal interface is opened only to sign in, or when
// asked for. Runs, a run and usage are views of what seatbelt reports.
// Everything that comes from a ledger, a model or a tool is untrusted: it is only ever set as
// textContent, never as HTML.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { ChatPane } from "./chat";
import { basename, button, byId, el, plain } from "./dom";
import { showRun, showRuns, showUsage } from "./records";
import { TermPane } from "./terminal";
import type { Cli, Run, Tools } from "./types";
import "./style.css";

type State = "starting" | "running" | "ended";
type Session = {
  cli: Cli;
  folder: string;
  item: HTMLLIElement;
  pane: HTMLDivElement;
  head: HTMLDivElement;
  body: HTMLDivElement;
  chat: ChatPane | null;
  term: TermPane | null;
  state: State;
  note: string;
  /** Ended by the user: nothing starts again. */
  closing: boolean;
  /** The terminal is open to sign in, rather than because it was asked for. */
  signingIn: boolean;
};
type View =
  | { kind: "session"; session: Session }
  | { kind: "runs" }
  | { kind: "run"; run: Run }
  | { kind: "usage" }
  | { kind: "empty" };

const sessions: Session[] = [];
let tools: Tools | null = null;
let folder: string | null = null;
let view: View = { kind: "empty" };

// -- tools and set-up --------------------------------------------------------------------

async function check(): Promise<void> {
  try {
    tools = await invoke<Tools>("status");
  } catch (e) {
    tools = null;
    showSetup(`Could not look for seatbelt: ${plain(e)}`, "");
    return;
  }
  const sb = tools.seatbelt;
  byId("mode").textContent = sb ? `${sb.version}` : "";
  if (!sb) {
    showSetup("seatbelt is not installed, or not on your PATH. Install it with:", "uv tool install seatbelt-ai");
  } else if (!sb.supported) {
    showSetup(`seatbelt ${sb.version} is too old for this app. Upgrade it with:`, "uv tool upgrade seatbelt-ai");
  } else {
    byId("setup").hidden = true;
  }
  renderCliButtons();
  if (folder === null) await setFolder(remembered() ?? tools.home ?? "", false);
}

function showSetup(text: string, command: string): void {
  byId("setup").hidden = false;
  byId("setup-text").textContent = text;
  const cmd = byId("setup-command");
  cmd.textContent = command;
  cmd.hidden = command === "";
}

function ready(): boolean {
  return Boolean(tools?.seatbelt?.supported) && folder !== null;
}

// -- the folder a session starts in: picked, typed or dropped; checked by the app; recent ones
// remembered on this computer --------------------------------------------------------------

const FOLDER_KEY = "seatbelt.folder";
const RECENT_KEY = "seatbelt.recentFolders";
const RECENT_MAX = 6;

function recentFolders(): string[] {
  try {
    const list: unknown = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(list) ? list.filter((p): p is string => typeof p === "string").slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}

function saveRecent(list: string[]): void {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
  } catch {
    // not remembered this time; nothing else depends on it
  }
  renderRecents();
}

function renderRecents(): void {
  const box = byId("recent-folders");
  const list = recentFolders().filter((p) => p !== folder);
  box.hidden = list.length === 0;
  box.replaceChildren(el("span", "muted small", "Recent"));
  for (const path of list) {
    const chip = button(basename(path), "recent", () => void setFolder(path), path);
    box.append(chip);
  }
}

async function browse(): Promise<void> {
  const b = byId<HTMLButtonElement>("browse");
  b.disabled = true;
  try {
    const picked = await invoke<string | null>("pick_folder", { start: folder });
    if (picked) await setFolder(picked);
  } catch (e) {
    const error = byId("folder-error");
    error.textContent = plain(e);
    error.hidden = false;
  } finally {
    b.disabled = false;
  }
}

function remembered(): string | null {
  try {
    return localStorage.getItem(FOLDER_KEY);
  } catch {
    return null; // storage can be unavailable; the home folder is the default then
  }
}

/** A long path shows its end, the folder's own name, rather than its start. */
function showEnd(input: HTMLInputElement): void {
  requestAnimationFrame(() => {
    if (document.activeElement !== input) input.scrollLeft = input.scrollWidth;
  });
}

/** Check `path` and make it the folder; false, with the reason shown, if it cannot be. */
async function setFolder(path: string, say = true): Promise<boolean> {
  const error = byId("folder-error");
  const input = byId<HTMLInputElement>("folder-input");
  try {
    folder = await invoke<string>("check_folder", { path });
    input.value = folder;
    input.title = folder;
    showEnd(input);
    error.hidden = true;
    try {
      localStorage.setItem(FOLDER_KEY, folder);
    } catch {
      // not remembered this time; nothing else depends on it
    }
    const chosen = folder;
    saveRecent([chosen, ...recentFolders().filter((p) => p !== chosen)]);
    updateCliButtons();
    return true;
  } catch (e) {
    if (say) {
      error.textContent = plain(e);
      error.hidden = false;
    }
    // a recent folder that has gone is forgotten
    if (recentFolders().includes(path)) saveRecent(recentFolders().filter((p) => p !== path));
    input.value = folder ?? "";
    updateCliButtons();
    return false;
  }
}

// The buttons are made once per check and only enabled or disabled after: one replaced while
// it is being clicked (when the folder field loses focus to it) would lose that click.
const cliButtons = new Map<string, { cli: Cli; button: HTMLButtonElement }>();

function renderCliButtons(): void {
  const box = byId("cli-buttons");
  box.replaceChildren();
  cliButtons.clear();
  for (const cli of tools?.clis ?? []) {
    const b = button(cli.label, "cli", () => void newSession(cli), cli.path ?? `${cli.name} was not found on your PATH`);
    cliButtons.set(cli.name, { cli, button: b });
    box.append(b);
  }
  updateCliButtons();
}

function updateCliButtons(): void {
  for (const { cli, button: b } of cliButtons.values()) b.disabled = !ready() || cli.path === null;
}

// -- sessions: a chat, or the CLI's own interface in a terminal ------------------------------

async function newSession(cli: Cli): Promise<void> {
  // a path typed but not yet applied (the click came first) is what the user means
  const typed = byId<HTMLInputElement>("folder-input").value;
  if (typed !== folder && !(await setFolder(typed))) return;
  if (!ready() || folder === null) return;
  const pane = el("div", "session");
  const head = el("div", "session-head");
  const body = el("div", "session-body");
  pane.append(head, body);
  byId("sessions-view").append(pane);
  const s: Session = {
    cli,
    folder,
    item: el("li"),
    pane,
    head,
    body,
    chat: null,
    term: null,
    state: "starting",
    note: "starting",
    closing: false,
    signingIn: false,
  };
  sessions.push(s);
  byId("tabs").append(s.item);
  byId("no-tabs").hidden = true;
  show({ kind: "session", session: s });
  await startChat(s);
}

function setState(s: Session, state: State, note: string): void {
  s.state = state;
  s.note = note;
  renderItem(s);
  renderHead(s);
}

async function startChat(s: Session): Promise<void> {
  s.closing = false;
  s.term?.dispose();
  s.term = null;
  s.chat?.dispose();
  const box = el("div", "chat-box");
  s.body.replaceChildren(box);
  const chat = new ChatPane(box, s.cli, {
    signIn: () => void openTerminal(s, true),
    changed: (state, note) => {
      if (s.chat === chat) setState(s, state, note);
    },
    ended: () => refreshRecords(),
  });
  s.chat = chat;
  await chat.start(s.folder);
  if (isShown(s)) chat.focus();
}

/** The CLI's own interface, through `seatbelt run` in a terminal: to sign in, or because it
 * was asked for. The chat ends first, and starts again (a new run) when the terminal ends. */
async function openTerminal(s: Session, signIn: boolean): Promise<void> {
  const chat = s.chat;
  s.chat = null;
  if (chat?.running) await chat.close();
  chat?.dispose();
  const box = el("div", "term");
  s.body.replaceChildren(box);
  s.signingIn = signIn;
  const term = new TermPane(box, s.cli, s.folder, (_code, report) => {
    if (s.term !== term) return;
    refreshRecords();
    if (s.closing) {
      setState(s, "ended", report?.recorded ? `recorded ${report.run}` : "ended");
    } else {
      void startChat(s);
    }
  });
  s.term = term;
  setState(s, "running", signIn ? "signing in" : "terminal");
  await term.start();
  if (isShown(s)) term.focus();
}

function renderItem(s: Session): void {
  const item = s.item;
  item.className = `tab ${s.state}${isShown(s) ? " active" : ""}`;
  const title = el("button", "tab-title");
  title.type = "button";
  title.append(el("span", "dot"), el("span", "tab-name", s.cli.label));
  title.append(el("span", "tab-note", `${basename(s.folder)} · ${s.note}`));
  title.addEventListener("click", () => show({ kind: "session", session: s }));
  const close = button(
    "×",
    "tab-close",
    () => void endSession(s),
    s.state === "ended" ? "Remove" : "End this session (its run is closed and signed)",
  );
  item.replaceChildren(title, close);
}

function renderHead(s: Session): void {
  const label = el("div", "session-title");
  label.append(el("strong", undefined, s.cli.label), el("span", "muted", ` · ${s.folder} · ${s.note}`));
  const actions = el("div", "session-actions");
  if (s.term && s.state !== "ended" && !s.closing) {
    label.append(
      el(
        "div",
        "session-hint",
        s.signingIn
          ? `Sign in with ${s.cli.label}'s own sign-in below, then leave it (/exit) to go back to the chat.`
          : `${s.cli.label}'s own interface. Leave it (/exit) or press Back to chat.`,
      ),
    );
    actions.append(
      button("Back to chat", "ghost", () => void s.term?.close(), "End the terminal session and go back to the chat"),
    );
  }
  if (s.chat && s.state !== "ended") {
    actions.append(
      button("Open in terminal", "ghost", () => void openTerminal(s, false), `${s.cli.label}'s own interface: to sign in, for example`),
    );
  }
  if (s.state === "ended") {
    actions.append(button("New chat", "ghost", () => void startChat(s), "Start again in this folder, as a new run"));
  } else {
    actions.append(button("End", "ghost danger", () => void endSession(s), "End the session; its run is closed and signed"));
  }
  s.head.replaceChildren(label, actions);
}

async function endSession(s: Session): Promise<void> {
  if (s.state !== "ended") {
    s.closing = true;
    setState(s, s.state, "ending");
    await (s.term ?? s.chat)?.close();
    return;
  }
  s.chat?.dispose();
  s.term?.dispose();
  s.pane.remove();
  s.item.remove();
  sessions.splice(sessions.indexOf(s), 1);
  byId("no-tabs").hidden = sessions.length > 0;
  if (view.kind === "session" && view.session === s) {
    const next = sessions[sessions.length - 1];
    show(next ? { kind: "session", session: next } : { kind: "empty" });
  }
}

// -- views ---------------------------------------------------------------------------------

function isShown(s: Session): boolean {
  return view.kind === "session" && view.session === s;
}

function show(next: View): void {
  view = next;
  byId("empty").hidden = next.kind !== "empty";
  byId("sessions-view").hidden = next.kind !== "session";
  const records = byId("records-view");
  records.hidden = !(next.kind === "runs" || next.kind === "run" || next.kind === "usage");
  for (const s of sessions) {
    s.pane.hidden = !isShown(s);
    renderItem(s);
    renderHead(s);
  }
  byId("nav-runs").classList.toggle("active", next.kind === "runs" || next.kind === "run");
  byId("nav-usage").classList.toggle("active", next.kind === "usage");
  if (next.kind === "session") {
    next.session.term?.focus();
    next.session.chat?.focus();
  } else if (next.kind === "runs") {
    void showRuns(records, (run) => show({ kind: "run", run }));
  } else if (next.kind === "run") {
    void showRun(records, next.run, () => show({ kind: "runs" }));
  } else if (next.kind === "usage") {
    void showUsage(records);
  }
}

/** A run has ended: the runs or usage view on screen counts it. */
function refreshRecords(): void {
  if (view.kind === "runs" || view.kind === "usage") show(view);
}

// -- wiring ------------------------------------------------------------------------------

byId("recheck").addEventListener("click", () => void check());
byId("browse").addEventListener("click", () => void browse());
renderRecents();
byId("nav-runs").addEventListener("click", () => show({ kind: "runs" }));
byId("nav-usage").addEventListener("click", () => show({ kind: "usage" }));
const folderInput = byId<HTMLInputElement>("folder-input");
folderInput.addEventListener("blur", () => showEnd(folderInput));
folderInput.addEventListener("change", () => {
  if (folderInput.value !== folder) void setFolder(folderInput.value);
});
folderInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    void setFolder(folderInput.value);
  }
});
void getCurrentWebview().onDragDropEvent((event) => {
  const kind = event.payload.type;
  document.body.classList.toggle("dropping", kind === "enter" || kind === "over");
  if (kind === "drop" && event.payload.paths.length > 0) void setFolder(event.payload.paths[0]);
});

// -- quitting while sessions run ----------------------------------------------------------

void listen<number>("quit-requested", (event) => {
  const n = event.payload;
  byId("quit-text").textContent =
    n === 1
      ? "1 session is still running. Quitting ends it and closes its run."
      : `${n} sessions are still running. Quitting ends them and closes their runs.`;
  for (const id of ["quit-ok", "quit-cancel"]) byId<HTMLButtonElement>(id).disabled = false;
  byId("quit").hidden = false;
  byId("quit-cancel").focus();
});
byId("quit-cancel").addEventListener("click", () => {
  byId("quit").hidden = true;
});
byId("quit-ok").addEventListener("click", () => {
  for (const id of ["quit-ok", "quit-cancel"]) byId<HTMLButtonElement>(id).disabled = true;
  byId("quit-text").textContent = "Ending sessions and closing their runs…";
  void invoke("quit");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !byId("quit").hidden) byId("quit-cancel").click();
});

void check();
