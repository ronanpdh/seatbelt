// The Seatbelt window: sessions on the left, each a terminal running `seatbelt run <cli>`.
// Everything that comes from a ledger (run names, above all) is untrusted: it is only ever
// set as textContent, never as HTML.

import { Channel, invoke } from "@tauri-apps/api/core";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import "./style.css";

type Cli = { name: string; label: string; path: string | null };
type Seatbelt = { path: string; version: string; supported: boolean };
type Tools = { seatbelt: Seatbelt | null; clis: Cli[]; home: string | null };
type Report = {
  run: string;
  cli: string;
  recorded_by: "local" | "gateway";
  gateway: string | null;
  recorded: boolean;
  ledgers: string[];
  pages: string[];
  exit: number;
};
type TabEvent =
  | { kind: "output"; data: string }
  | { kind: "exit"; code: number | null; report: Report | null };
type Run = {
  name: string;
  id: string;
  started: string | null;
  model_calls: number | null;
  status: "ended" | "open" | "unreadable";
  ok: boolean | null;
  ledger: string;
  page: string | null;
};
type Listing = { folder: string; total: number; runs: Run[] };

type Tab = {
  id: number | null;
  cli: Cli;
  folder: string;
  term: Terminal;
  fit: FitAddon;
  box: HTMLDivElement;
  item: HTMLLIElement;
  state: "starting" | "running" | "ended";
  note: string;
};

const byId = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
const tabs: Tab[] = [];
let tools: Tools | null = null;
let folder: string | null = null;
let active: Tab | null = null;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// errors are ours, but may quote a path: no control characters into the terminal
const plain = (text: unknown): string => String(text).replace(/[\u0000-\u001f\u007f-\u009f]/g, " ");
const basename = (path: string): string => path.split(/[\\/]/).filter(Boolean).pop() ?? path;

const dark = window.matchMedia("(prefers-color-scheme: dark)");
const themes = {
  light: { background: "#ffffff", foreground: "#1b1f24", cursor: "#1b1f24", selectionBackground: "#c8d9f5" },
  dark: { background: "#0d1117", foreground: "#e6edf3", cursor: "#e6edf3", selectionBackground: "#264f78" },
};
const termTheme = () => (dark.matches ? themes.dark : themes.light);
dark.addEventListener("change", () => tabs.forEach((t) => (t.term.options.theme = termTheme())));

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
  if (folder === null) folder = tools.home;
  renderFolder();
  renderCliButtons();
  await refreshRuns();
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

function renderFolder(): void {
  byId("folder-name").textContent = folder ? basename(folder) : "choose a folder";
  byId("folder").title = folder ?? "";
}

function renderCliButtons(): void {
  const box = byId("cli-buttons");
  box.replaceChildren();
  for (const cli of tools?.clis ?? []) {
    const button = el("button", "cli", cli.label);
    button.type = "button";
    button.disabled = !ready() || cli.path === null;
    button.title = cli.path ?? `${cli.name} was not found on your PATH`;
    button.addEventListener("click", () => void openTab(cli));
    box.append(button);
  }
}

// -- sessions ----------------------------------------------------------------------------

async function openTab(cli: Cli): Promise<void> {
  if (!ready() || folder === null) return;
  const box = el("div", "term");
  byId("terminals").append(box);
  const term = new Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: 13,
    scrollback: 10000,
    theme: termTheme(),
  });
  const fit = new FitAddon();
  term.loadAddon(fit); // no clipboard or links addon: output cannot write the clipboard or open links
  term.open(box);
  const tab: Tab = {
    id: null,
    cli,
    folder,
    term,
    fit,
    box,
    item: el("li"),
    state: "starting",
    note: "starting",
  };
  tabs.push(tab);
  renderTabItem(tab);
  byId("tabs").append(tab.item);
  byId("no-tabs").hidden = true;
  activate(tab);

  const events = new Channel<TabEvent>();
  events.onmessage = (event) => {
    if (event.kind === "output") term.write(event.data);
    else ended(tab, event.code, event.report);
  };
  term.onData((data) => {
    if (tab.id !== null && tab.state === "running") {
      invoke("write_tab", { id: tab.id, data }).catch(() => undefined);
    }
  });
  term.onResize(({ cols, rows }) => {
    if (tab.id !== null && tab.state === "running") {
      invoke("resize_tab", { id: tab.id, cols, rows }).catch(() => undefined);
    }
  });
  try {
    tab.id = await invoke<number>("open_tab", {
      cli: cli.name,
      cwd: tab.folder,
      cols: term.cols,
      rows: term.rows,
      events,
    });
    if (tab.state === "starting") {
      tab.state = "running";
      tab.note = "recording";
    }
  } catch (e) {
    term.write(`\r\n${plain(e)}\r\n`);
    tab.state = "ended";
    tab.note = "could not start";
  }
  renderTabItem(tab);
}

function ended(tab: Tab, code: number | null, report: Report | null): void {
  tab.state = "ended";
  if (report?.recorded) {
    tab.note = `recorded ${report.run}`;
  } else if (report) {
    tab.note = "nothing recorded";
  } else {
    tab.note = code === null ? "ended" : `ended (${code})`;
  }
  tab.term.write(`\r\n\x1b[2m[session ended: ${plain(tab.note)}]\x1b[0m\r\n`);
  renderTabItem(tab);
  void refreshRuns();
}

function renderTabItem(tab: Tab): void {
  const item = tab.item;
  item.className = `tab ${tab.state}${tab === active ? " active" : ""}`;
  const title = el("button", "tab-title");
  title.type = "button";
  title.append(el("span", "dot"), el("span", "tab-name", tab.cli.label));
  title.append(el("span", "tab-note", `${basename(tab.folder)} · ${tab.note}`));
  title.addEventListener("click", () => activate(tab));
  const close = el("button", "tab-close", "×");
  close.type = "button";
  close.title = tab.state === "ended" ? "Remove" : "End this session (its run is closed and signed)";
  close.addEventListener("click", () => void closeTab(tab));
  item.replaceChildren(title, close);
}

function activate(tab: Tab): void {
  active = tab;
  byId("empty").hidden = true;
  for (const t of tabs) {
    t.box.hidden = t !== tab;
    renderTabItem(t);
  }
  requestAnimationFrame(() => {
    tab.fit.fit();
    tab.term.focus();
  });
}

async function closeTab(tab: Tab): Promise<void> {
  if (tab.state !== "ended" && tab.id !== null) {
    tab.note = "ending";
    renderTabItem(tab);
    await invoke("close_tab", { id: tab.id }).catch(() => undefined);
    return; // removed from the list once it has ended, by the next click
  }
  tab.term.dispose();
  tab.box.remove();
  tab.item.remove();
  tabs.splice(tabs.indexOf(tab), 1);
  if (active === tab) {
    active = null;
    const next = tabs[tabs.length - 1];
    if (next) activate(next);
    else byId("empty").hidden = false;
  }
  byId("no-tabs").hidden = tabs.length > 0;
}

new ResizeObserver(() => active?.fit.fit()).observe(byId("terminals"));

// -- runs --------------------------------------------------------------------------------

async function refreshRuns(): Promise<void> {
  const list = byId("runs");
  const note = byId("runs-note");
  if (!tools?.seatbelt?.supported) {
    list.replaceChildren();
    note.textContent = "";
    return;
  }
  let listing: Listing;
  try {
    listing = await invoke<Listing>("list_runs");
  } catch (e) {
    note.textContent = plain(e);
    return;
  }
  list.replaceChildren(...listing.runs.map(runItem));
  note.textContent =
    listing.total === 0
      ? "No runs yet."
      : `${listing.total} run${listing.total === 1 ? "" : "s"} in ${listing.folder}`;
}

function runItem(run: Run): HTMLLIElement {
  const item = el("li", `run ${run.status}${run.ok === false ? " failed" : ""}`);
  const button = el("button", "run-button");
  button.type = "button";
  button.disabled = run.page === null;
  button.title = run.page ? "Open this run's page in your browser" : "This run has no page yet";
  button.append(el("span", "run-name", run.name));
  const when = run.started ? new Date(run.started).toLocaleString() : "";
  const state = run.status === "ended" ? (run.ok === false ? "failed" : "") : run.status;
  const calls = run.model_calls === null ? "" : `${run.model_calls} model call${run.model_calls === 1 ? "" : "s"}`;
  button.append(el("span", "run-meta", [when, calls, state].filter(Boolean).join(" · ")));
  button.addEventListener("click", () => {
    invoke("open_page", { id: run.id }).catch((e) => {
      byId("runs-note").textContent = plain(e);
    });
  });
  item.append(button);
  return item;
}

// -- wiring ------------------------------------------------------------------------------

byId("recheck").addEventListener("click", () => void check());
byId("refresh").addEventListener("click", () => void refreshRuns());
byId("folder").addEventListener("click", async () => {
  const picked = await invoke<string | null>("pick_folder").catch(() => null);
  if (picked) {
    folder = picked;
    renderFolder();
    renderCliButtons();
  }
});
window.addEventListener("focus", () => void refreshRuns());

void check();
