// Settings: seatbelt's setup and each CLI's version and sign-in, asked when the view opens and
// on "Check again"; and signing a CLI in again, in a terminal here. seatbelt's checks are
// `seatbelt doctor`; each CLI is asked by its own commands (settings.rs). What seatbelt or a
// CLI says is set as textContent only.

import { invoke } from "@tauri-apps/api/core";
import { button, el, plain } from "./dom";
import { TermPane } from "./terminal";
import type { Cli, CliStatus, Doctor, Tools } from "./types";

type Hooks = {
  /** Look for seatbelt and the CLIs again, as the sidebar does, and say what was found. */
  recheck: () => Promise<Tools | null>;
  /** The folder Gemini CLI's sign-in opens in, through `seatbelt run`. */
  folder: () => string;
  /** A sign-in through `seatbelt run` has ended having recorded a run. */
  recorded: () => void;
};

type Fact = [name: string, value: string, css?: string];

type Card = {
  cli: Cli;
  card: HTMLElement;
  status: HTMLElement;
  signIn: HTMLButtonElement;
  login: HTMLElement;
  /** The sign-in's terminal on screen, running or ended. */
  term: TermPane | null;
  /** The status last asked for: one that took longer does not replace it. */
  asked: number;
};

function facts(rows: Fact[]): HTMLDListElement {
  const dl = el("dl", "facts");
  for (const [name, value, css] of rows) dl.append(el("dt", undefined, name), el("dd", css, value));
  return dl;
}

export class SettingsView {
  private readonly again: HTMLButtonElement;
  private readonly seatbelt = el("section", "card");
  private readonly clis = el("div", "settings-clis");
  private readonly cards = new Map<string, Card>();
  private latest = 0;

  constructor(
    view: HTMLElement,
    private readonly hooks: Hooks,
  ) {
    this.again = button("Check again", "ghost", () => void this.check(), "Look for seatbelt and each CLI again, and ask each what it has");
    const head = el("header", "view-head");
    head.append(el("h1", undefined, "Settings"), this.again);
    view.append(
      head,
      el("p", "muted", "What seatbelt and each CLI say on this computer, asked when this view opens and when you press Check again."),
      this.seatbelt,
      el("h2", "settings-section", "Sign-in"),
      this.clis,
    );
  }

  /** The view is on screen: a sign-in's terminal fits it again, and everything is asked again. */
  opened(): void {
    for (const c of this.cards.values()) c.term?.fit();
    void this.check();
  }

  async check(): Promise<void> {
    const mine = ++this.latest;
    this.again.disabled = true;
    this.showSeatbelt("", "Checking…", []);
    const tools = await this.hooks.recheck();
    if (mine !== this.latest) return;
    const asked = (tools?.clis ?? []).map((cli) => this.checkCli(this.card(cli)));
    await Promise.all([this.checkSeatbelt(tools, mine), ...asked]);
    if (mine === this.latest) this.again.disabled = false;
  }

  // -- seatbelt ----------------------------------------------------------------------------

  private showSeatbelt(css: string, verdict: string, rows: Fact[], note?: string): void {
    this.seatbelt.className = css ? `card ${css}` : "card";
    this.seatbelt.replaceChildren(el("h2", undefined, "seatbelt"), el("p", "verdict", verdict), facts(rows));
    if (note) this.seatbelt.append(el("p", "muted small", note));
  }

  private async checkSeatbelt(tools: Tools | null, mine: number): Promise<void> {
    const sb = tools?.seatbelt;
    if (!tools) return this.showSeatbelt("bad", "Not checked", [], "The app could not look for seatbelt.");
    if (!sb) return this.showSeatbelt("bad", "Not installed", [["Install it with", "uv tool install seatbelt-ai", "mono"]]);
    const rows: Fact[] = [
      ["Installed at", sb.path, "mono"],
      ["Version", sb.version],
    ];
    let doctor: Doctor | string;
    try {
      doctor = await invoke<Doctor>("seatbelt_checks");
    } catch (e) {
      doctor = plain(e);
    }
    if (mine !== this.latest) return;
    if (typeof doctor === "string") return this.showSeatbelt("bad", "Could not check", [...rows, ["Why", doctor]]);
    const pdf = doctor.pdf;
    rows.push(
      ["Runs folder", doctor.runs ?? "not known", "mono"],
      ["Signing key", doctor.key ?? "none yet: the first local run makes one", doctor.key ? "mono" : undefined],
      ["Config", doctor.config ?? "reads", doctor.config ? "bad" : undefined],
      ["PDF of a run", pdf.ok ? `yes, with WeasyPrint ${pdf.weasyprint ?? ""}` : (pdf.reason ?? "no"), pdf.ok ? undefined : "warn"],
    );
    if (doctor.config || !sb.supported) {
      const note = sb.supported ? undefined : "This seatbelt is too old for this app: uv tool upgrade seatbelt-ai";
      return this.showSeatbelt("bad", "Needs fixing", rows, note);
    }
    this.showSeatbelt(pdf.ok ? "good" : "warn", pdf.ok ? "Ready" : "Ready, but cannot make a PDF", rows);
  }

  // -- each CLI's sign-in ------------------------------------------------------------------

  private card(cli: Cli): Card {
    const known = this.cards.get(cli.name);
    if (known) {
      known.cli = cli;
      return known;
    }
    const card = el("section", "card");
    const status = el("div");
    const login = el("div", "login-term");
    login.hidden = true;
    const c: Card = { cli, card, status, signIn: button("Sign in again", "primary", () => void this.signIn(c)), login, term: null, asked: 0 };
    const actions = el("div", "view-actions settings-actions");
    actions.append(c.signIn);
    card.append(el("h2", undefined, cli.label), status, actions, login);
    this.clis.append(card);
    this.cards.set(cli.name, c);
    return c;
  }

  private async checkCli(c: Card): Promise<void> {
    const mine = ++c.asked;
    c.card.className = "card";
    c.status.replaceChildren(el("p", "verdict", "Checking…"));
    c.signIn.disabled = true;
    let status: CliStatus | string;
    try {
      status = await invoke<CliStatus>("cli_status", { cli: c.cli.name });
    } catch (e) {
      status = plain(e);
    }
    if (mine !== c.asked) return;
    let verdict: string;
    let css: string;
    let rows: Fact[];
    if (typeof status === "string") {
      [verdict, css, rows] = ["Could not check", "bad", [["Why", status]]];
    } else if (status.path === null) {
      [verdict, css, rows] = ["Not found", "bad", [["Where", `${status.name} is not installed, or not on your PATH`]]];
    } else {
      const known = status.signed_in;
      verdict = known === true ? "Signed in" : known === false ? "Not signed in" : "Sign-in not known";
      css = known === true ? "good" : known === false ? "bad" : "warn";
      rows = [
        ["Installed at", status.path, "mono"],
        ["Version", status.version ?? "it did not say"],
        ["Sign-in", status.sign_in],
        ["Sign in again runs", status.login, "mono"],
      ];
      c.signIn.title = status.login;
    }
    c.card.className = `card ${css}`;
    c.status.replaceChildren(el("p", "verdict", verdict), facts(rows));
    if (typeof status !== "string" && status.path !== null && status.note) {
      c.status.append(el("p", "muted small", status.note));
    }
    c.signIn.disabled = Boolean(c.term?.running) || typeof status === "string" || status.path === null;
  }

  /** The CLI's own sign-in, in a terminal in its card; its status is asked again when it ends. */
  private async signIn(c: Card): Promise<void> {
    if (c.term?.running) return;
    c.term?.dispose(); // the last sign-in's, ended
    c.signIn.disabled = true;
    const bar = el("div", "view-actions");
    const box = el("div", "term");
    bar.append(
      el("span", "muted small", `${c.cli.label}'s own sign-in (${c.signIn.title}). Follow what it says below; it may open your browser.`),
      button("Stop", "ghost danger", () => void c.term?.close(), "End the sign-in"),
    );
    c.login.replaceChildren(bar, box);
    c.login.hidden = false;
    const term = new TermPane(
      box,
      c.cli,
      this.hooks.folder(),
      (_code, report) => {
        if (c.term !== term) return;
        if (report?.recorded) this.hooks.recorded();
        bar.replaceChildren(
          el("span", "muted small", "The sign-in has ended: its status above has been asked again."),
          button("Close", "ghost", () => {
            term.dispose();
            if (c.term === term) c.term = null;
            c.login.replaceChildren();
            c.login.hidden = true;
          }),
        );
        void this.checkCli(c);
      },
      null,
      true,
    );
    c.term = term;
    await term.start();
    if (c.term === term) term.focus();
  }
}
