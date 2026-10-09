// Runs, a run, and usage, as seatbelt reports them: `runs --json`, `reconstruct --json`,
// `verify --json` and `report --json`. The app never reads a ledger itself. Everything that
// came from a ledger is set as textContent only.

import { invoke } from "@tauri-apps/api/core";
import { button, count, el, number, plain, table, when } from "./dom";
import type { Fleet, Listing, Page, Row, Run, Usage, Verification } from "./types";

const CLIS: [string, string][] = [
  ["claude", "Claude Code"],
  ["codex", "Codex"],
  ["gemini", "Gemini CLI"],
];

function cliOf(run: Run): string {
  return CLIS.find(([name]) => run.name.startsWith(`${name}-`))?.[0] ?? "other";
}

/** How the run ended. */
function state(run: Run): { label: string; css: string } {
  if (run.status === "unreadable") return { label: "unreadable", css: "bad" };
  if (run.status === "open") return { label: "open", css: "warn" };
  return run.ok === false ? { label: "failed", css: "bad" } : { label: "ok", css: "good" };
}

/** What `seatbelt verify` would say of it, from `seatbelt runs`. */
function check(run: Run): { label: string; css: string } {
  if (run.chain === null) return { label: "unreadable", css: "bad" };
  if (run.chain === "broken") return { label: "broken", css: "bad" };
  if (run.signature === "forged") return { label: "forged", css: "bad" };
  if (run.signature === "attested") return { label: "verified", css: "good" };
  if (run.signature === "unchecked") return { label: "unchecked", css: "warn" };
  return { label: "unsigned", css: "warn" };
}

const badge = (label: string, css: string): HTMLSpanElement => el("span", `pill ${css}`, label);

function heading(title: string, ...extra: Node[]): HTMLElement {
  const h = el("header", "view-head");
  h.append(el("h1", undefined, title), ...extra);
  return h;
}

// -- runs: search and filters over `seatbelt runs --json` ----------------------------------

type Filters = { text: string; state: string; check: string; cli: string; since: string };
const filters: Filters = { text: "", state: "all", check: "all", cli: "all", since: "any" };

function select(options: [string, string][], value: string, onChange: (v: string) => void, label: string): HTMLSelectElement {
  const s = el("select");
  s.setAttribute("aria-label", label);
  for (const [v, text] of options) {
    const o = el("option", undefined, text);
    o.value = v;
    s.append(o);
  }
  s.value = value;
  s.addEventListener("change", () => onChange(s.value));
  return s;
}

function matches(run: Run, f: Filters, now: number): boolean {
  const text = f.text.trim().toLowerCase();
  if (text && ![run.name, run.id, run.client ?? ""].some((v) => v.toLowerCase().includes(text))) return false;
  if (f.state !== "all" && state(run).label !== f.state) return false;
  if (f.check !== "all" && check(run).label !== f.check) return false;
  if (f.cli !== "all" && cliOf(run) !== f.cli) return false;
  if (f.since !== "any") {
    const started = run.started ? new Date(run.started).getTime() : NaN;
    if (Number.isNaN(started)) return false;
    const day = 24 * 3600 * 1000;
    const limit =
      f.since === "today" ? new Date(new Date(now).toDateString()).getTime() : now - Number(f.since) * day;
    if (started < limit) return false;
  }
  return true;
}

/** The view last asked for: one that took longer to load does not replace it. */
let latest = 0;
const current = (mine: number): boolean => mine === latest;

export async function showRuns(view: HTMLElement, open: (run: Run) => void): Promise<void> {
  const mine = ++latest;
  view.replaceChildren(heading("Runs"), el("p", "muted", "Loading…"));
  let listing: Listing;
  try {
    listing = await invoke<Listing>("list_runs");
  } catch (e) {
    if (current(mine)) view.replaceChildren(heading("Runs"), el("p", "error", plain(e)));
    return;
  }
  if (!current(mine)) return;
  const refresh = button("↻ Refresh", "ghost", () => void showRuns(view, open), "List the runs again");
  const search = el("input", "search");
  search.type = "search";
  search.placeholder = "Search by run, id or client";
  search.value = filters.text;
  search.setAttribute("aria-label", "Search runs");
  const bar = el("div", "filters");
  const list = el("div", "list-box");
  const shown = el("p", "muted small");
  const draw = () => {
    const now = Date.now();
    const runs = listing.runs.filter((r) => matches(r, filters, now));
    shown.textContent =
      `${count(runs.length, "run")} shown of ${listing.runs.length}` +
      (listing.total > listing.runs.length ? ` (the newest of ${listing.total})` : "") +
      ` · ${listing.folder}`;
    if (listing.runs.length === 0) {
      list.replaceChildren(el("p", "muted", "No runs yet. Start a session; each one is recorded as a run."));
      return;
    }
    const rows = runs.map((run) => {
      const name = el("button", "link-button", run.name);
      name.type = "button";
      name.addEventListener("click", () => open(run));
      const s = state(run);
      const c = check(run);
      return [
        name,
        when(run.started),
        run.model_calls === null ? "" : number(run.model_calls),
        badge(s.label, s.css),
        badge(c.label, c.css),
        run.client ?? "",
      ];
    });
    list.replaceChildren(
      runs.length
        ? table(["Run", "Started", "Model calls", "Outcome", "Verified", "Client"], rows, "grid runs-table")
        : el("p", "muted", "No run matches."),
    );
  };
  search.addEventListener("input", () => {
    filters.text = search.value;
    draw();
  });
  bar.append(
    search,
    select(
      [["all", "Any outcome"], ["ok", "OK"], ["failed", "Failed"], ["open", "Open"], ["unreadable", "Unreadable"]],
      filters.state,
      (v) => ((filters.state = v), draw()),
      "Outcome",
    ),
    select(
      [
        ["all", "Any verification"],
        ["verified", "Verified"],
        ["unsigned", "Unsigned"],
        ["unchecked", "Unchecked"],
        ["broken", "Broken"],
        ["forged", "Forged"],
      ],
      filters.check,
      (v) => ((filters.check = v), draw()),
      "Verification",
    ),
    select([["all", "Any CLI"], ...CLIS, ["other", "Other"]], filters.cli, (v) => ((filters.cli = v), draw()), "CLI"),
    select(
      [["any", "Any time"], ["today", "Today"], ["7", "Last 7 days"], ["30", "Last 30 days"]],
      filters.since,
      (v) => ((filters.since = v), draw()),
      "Started",
    ),
  );
  view.replaceChildren(heading("Runs", refresh), bar, shown, list);
  draw();
  search.focus();
}

// -- one run: what its page shows, and its verification ----------------------------------

function verificationCard(v: Verification | string): HTMLElement {
  const card = el("section", "card verify");
  card.append(el("h2", undefined, "Verification"));
  if (typeof v === "string") {
    card.classList.add("bad");
    card.append(el("p", undefined, v));
    return card;
  }
  const signature: Record<string, string> = {
    attested: "signed, and the signature verifies with this machine's key",
    unchecked: "signed, but there is no key to check who signed it",
    unattested: "not signed",
    forged: "the signature does not match: forged or altered",
  };
  let headline: string;
  if (v.chain === "broken") {
    card.classList.add("bad");
    headline = v.first_bad_seq === null ? "Broken" : `Broken at event ${v.first_bad_seq}`;
  } else if (v.signature === "forged") {
    card.classList.add("bad");
    headline = "Forged";
  } else if (v.ok) {
    card.classList.add(v.signature === "attested" ? "good" : "warn");
    headline = "Verified";
  } else {
    card.classList.add("warn");
    headline = v.complete ? "Not verified" : "Incomplete";
  }
  card.append(el("p", "verdict", headline));
  const facts = el("dl", "facts");
  const fact = (k: string, val: string) => facts.append(el("dt", undefined, k), el("dd", undefined, val));
  fact("Hash chain", v.chain === "intact" ? `intact, ${count(v.events, "event")}` : "broken");
  fact(
    "Ended",
    v.chain === "broken" ? "not checked" : v.complete ? "yes, with a matching run.end" : "no run.end: still running, or cut short",
  );
  fact("Signature", v.signature ? signature[v.signature] ?? v.signature : "not checked");
  if (v.reason) fact("Why", v.reason);
  card.append(facts);
  return card;
}

function usageTable(models: Record<string, Usage>, total?: Usage, first = "Model"): HTMLTableElement {
  const rows: string[][] = Object.entries(models).map(([name, u]) => [
    name,
    number(u.calls),
    number(u.input_tokens),
    number(u.cache_read_input_tokens),
    number(u.cache_creation_input_tokens),
    number(u.output_tokens),
  ]);
  if (total) {
    rows.push([
      "Total",
      number(total.calls),
      number(total.input_tokens),
      number(total.cache_read_input_tokens),
      number(total.cache_creation_input_tokens),
      number(total.output_tokens),
    ]);
  }
  return table([first, "Calls", "In", "Cache read", "Cache write", "Out"], rows, "grid num");
}

function eventRow(row: Row): HTMLElement {
  const item = el("details", `event ${row.css}${row.denied ? " denied" : ""}${row.failed ? " failed" : ""}`);
  const summary = el("summary");
  summary.append(
    el("span", "seq", String(row.seq)),
    el("span", "time", row.time),
    el("span", "kind", row.kind),
    el("span", "what", row.what),
  );
  item.append(summary);
  // the detail is built on first opening: a long run has thousands of events
  item.addEventListener(
    "toggle",
    () => {
      if (!item.open || item.dataset.built) return;
      item.dataset.built = "1";
      const meta = el("dl", "facts small");
      for (const [k, v] of [["Actor", row.actor], ["Id", row.id], ["Parent", row.parent], ["Hash", row.hash], ["Previous", row.prev_hash]]) {
        if (v) meta.append(el("dt", undefined, k), el("dd", "mono", v));
      }
      const pre = el("pre", "detail");
      pre.append(document.createTextNode(row.detail));
      if (row.cut > 0) {
        pre.append(el("span", "cut", `\n… ${number(row.cut)} bytes left out; the ledger has them all …\n`));
        pre.append(document.createTextNode(row.detail_end));
      }
      item.append(meta, pre);
    },
  );
  return item;
}

export async function showRun(view: HTMLElement, run: Run, back: () => void): Promise<void> {
  const top = el("div", "view-actions");
  top.append(button("← Runs", "ghost", back));
  if (run.page) {
    top.append(
      button("Open its page in the browser", "ghost", () => {
        invoke("open_page", { id: run.id }).catch((e) => top.append(el("span", "error small", plain(e))));
      }),
    );
  }
  const mine = ++latest;
  view.replaceChildren(top, heading(run.name), el("p", "muted", "Checking and loading…"));
  const [verified, page] = await Promise.all([
    invoke<Verification>("verify_run", { id: run.id }).catch((e) => plain(e)),
    invoke<Page>("run_detail", { id: run.id }).catch((e) => plain(e)),
  ]);
  if (!current(mine)) return;
  const again = button("Verify again", "ghost", () => void showRun(view, run, back));
  top.append(again);
  const parts: Node[] = [top];
  if (typeof page === "string") {
    parts.push(heading(run.name), verificationCard(verified), el("p", "error", `No view of this run: ${page}`));
    view.replaceChildren(...parts);
    return;
  }
  const outcome = page.outcome === "ok" ? "good" : page.outcome === "failed" ? "bad" : "warn";
  parts.push(heading(page.title, badge(page.outcome, outcome)));
  if (page.outcome_reason) parts.push(el("p", "error", page.outcome_reason));

  const about = el("section", "card");
  about.append(el("h2", undefined, "Run"));
  const facts = el("dl", "facts");
  const fact = (k: string, v: string, mono = false) => {
    if (v) facts.append(el("dt", undefined, k), el("dd", mono ? "mono" : undefined, v));
  };
  fact("Run id", page.run_id, true);
  fact("Started", page.started);
  fact("Ended", page.ended || "not yet");
  fact("Duration", page.duration);
  fact("Agent", page.agent);
  fact("Client", page.client);
  fact("Principal", page.principal);
  fact("Events", number(page.events));
  fact("Ledger", page.ledger_file, true);
  fact("Ledger SHA-256", page.ledger_sha256, true);
  fact("Final hash", page.final_hash, true);
  fact("Check it yourself", page.verify_command, true);
  about.append(facts);
  const cards = el("div", "cards");
  cards.append(verificationCard(verified), about);
  parts.push(cards);

  const usage = el("section", "card");
  usage.append(el("h2", undefined, "Model usage"));
  usage.append(
    Object.keys(page.models).length ? usageTable(page.models, page.total) : el("p", "muted", "No model calls."),
  );
  if (Object.keys(page.tools).length) {
    usage.append(el("h3", undefined, "Tools"));
    usage.append(table(["Tool", "Calls"], Object.entries(page.tools).map(([t, n]) => [t, number(n)]), "grid num"));
  }
  parts.push(usage);

  if (page.refusals.length || page.failures.length) {
    const problems = el("section", "card bad-edge");
    if (page.refusals.length) {
      problems.append(el("h2", undefined, `Refused (${page.refusals.length})`));
      problems.append(table(["Event", "Rule", "Reason"], page.refusals.map((r) => [String(r.seq), r.rule, r.reason])));
    }
    if (page.failures.length) {
      problems.append(el("h2", undefined, `Failed (${page.failures.length})`));
      problems.append(table(["Event", "Kind", "Error"], page.failures.map((f) => [String(f.seq), f.kind, f.error])));
    }
    parts.push(problems);
  }

  const events = el("section", "card");
  events.append(el("h2", undefined, "Events"));
  const bar = el("div", "filters");
  const search = el("input", "search");
  search.type = "search";
  search.placeholder = "Search events";
  search.setAttribute("aria-label", "Search events");
  const kinds = [...new Set(page.rows.map((r) => r.kind))].sort();
  let kind = "all";
  const list = el("div", "events");
  const shown = el("p", "muted small");
  const draw = () => {
    const text = search.value.trim().toLowerCase();
    const rows = page.rows.filter(
      (r) =>
        (kind === "all" || r.kind === kind) &&
        (!text || [r.what, r.kind, r.actor, r.detail].some((v) => v.toLowerCase().includes(text))),
    );
    shown.textContent = `${count(rows.length, "event")} shown of ${page.rows.length}`;
    list.replaceChildren(...rows.map(eventRow));
  };
  search.addEventListener("input", draw);
  bar.append(search, select([["all", "Every kind"], ...kinds.map((k): [string, string] => [k, k])], kind, (v) => ((kind = v), draw()), "Kind"));
  events.append(bar, shown, list);
  parts.push(events);
  view.replaceChildren(...parts);
  draw();
}

// -- usage across runs: `seatbelt report --json` -------------------------------------------

export async function showUsage(view: HTMLElement): Promise<void> {
  const refresh = button("↻ Refresh", "ghost", () => void showUsage(view), "Count again");
  const mine = ++latest;
  view.replaceChildren(heading("Usage", refresh), el("p", "muted", "Counting…"));
  let fleet: Fleet;
  try {
    fleet = await invoke<Fleet>("usage_report");
  } catch (e) {
    if (current(mine)) view.replaceChildren(heading("Usage", refresh), el("p", "error", plain(e)));
    return;
  }
  if (!current(mine)) return;
  const parts: Node[] = [heading("Usage", refresh)];
  parts.push(el("p", "muted", `${count(fleet.runs, "run")} counted on this machine, as seatbelt report counts them.`));
  const models = el("section", "card");
  models.append(el("h2", undefined, "By model"));
  models.append(Object.keys(fleet.by_model).length ? usageTable(fleet.by_model) : el("p", "muted", "No model calls yet."));
  const people = el("section", "card");
  people.append(el("h2", undefined, "By person"));
  people.append(
    Object.keys(fleet.by_principal).length
      ? table(
          ["Person", "Runs", "Calls", "In", "Out", "Denied"],
          Object.entries(fleet.by_principal).map(([p, u]) => [p, number(u.runs), number(u.calls), number(u.input_tokens), number(u.output_tokens), number(u.denials)]),
          "grid num",
        )
      : el("p", "muted", "No one yet."),
  );
  parts.push(models, people);
  if (Object.keys(fleet.by_tool).length) {
    const tools = el("section", "card");
    tools.append(el("h2", undefined, "By tool"));
    tools.append(table(["Tool", "Calls"], Object.entries(fleet.by_tool).map(([t, n]) => [t, number(n)]), "grid num"));
    parts.push(tools);
  }
  const groups: [keyof Fleet, string, string][] = [
    ["broken", "Broken: altered or unreadable; not counted", "bad"],
    ["forged", "Forged: the signature does not match; not counted", "bad"],
    ["unsigned", "Ended but unsigned; not counted", "bad"],
    ["missing", "Missing: a signature whose ledger is gone", "bad"],
    ["failed", "Failed", "warn"],
    ["incomplete", "Open or cut short", "warn"],
    ["unattested", "Not signed", "warn"],
  ];
  const problems = el("section", "card");
  problems.append(el("h2", undefined, "Runs to look at"));
  let any = false;
  for (const [key, label, css] of groups) {
    const ids = fleet[key] as string[];
    if (!ids.length) continue;
    any = true;
    problems.append(el("h3", css, `${label} (${ids.length})`));
    const ul = el("ul", "ids");
    for (const id of ids) ul.append(el("li", "mono", id));
    problems.append(ul);
  }
  if (!any) problems.append(el("p", "good", "None: every run counted is intact and ended."));
  parts.push(problems);
  view.replaceChildren(...parts);
}
