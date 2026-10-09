// What the Rust side and seatbelt's JSON give the window.

export type Cli = { name: string; label: string; path: string | null };
export type Seatbelt = { path: string; version: string; supported: boolean };
export type Tools = { seatbelt: Seatbelt | null; clis: Cli[]; home: string | null };

/** What `seatbelt run` recorded, from its run report. */
export type Report = {
  run: string;
  cli: string;
  recorded_by: "local" | "gateway";
  gateway: string | null;
  recorded: boolean;
  ledgers: string[];
  pages: string[];
  exit: number;
};

export type ToolStatus = "running" | "done" | "failed" | "declined";
/** A model a CLI offers, as its own list gives it. */
export type Model = {
  id: string;
  name: string;
  description: string;
  /** The reasoning efforts it takes; empty when there is no choice. */
  efforts: string[];
  default_effort: string | null;
};

/** A permission mode a chat offers: never one that runs every tool without asking. */
export type Mode = { id: string; name: string; description: string };

/** A model, and an effort (null: the model's own default), by the CLI's own ids. */
export type Choice = { model: string; effort: string | null };

export type ChatEvent =
  | { kind: "text"; id: string; delta: string }
  | { kind: "message"; id: string; text: string }
  | { kind: "tool"; id: string; name: string; detail: string; status: ToolStatus }
  | { kind: "tool_input"; id: string; input: string }
  | { kind: "tool_output"; id: string; output: string }
  | { kind: "thinking"; id: string; delta: string }
  | { kind: "approval"; id: string; tool: string; detail: string }
  | { kind: "resolved"; id: string }
  | { kind: "turn_end"; ok: boolean; error: string | null }
  | { kind: "sign_in"; reason: string }
  | { kind: "conversation"; id: string }
  | { kind: "resumed"; ok: boolean }
  | { kind: "models"; models: Model[]; model: string | null; effort: string | null; current: string | null }
  | { kind: "modes"; modes: Mode[]; mode: string | null; current: string | null }
  | { kind: "notice"; text: string }
  | { kind: "log"; text: string }
  | { kind: "exit"; code: number | null; report: Report | null };

/** `seatbelt runs --json` */
export type Run = {
  name: string;
  id: string;
  started: string | null;
  model_calls: number | null;
  status: "ended" | "open" | "unreadable";
  ok: boolean | null;
  client: string | null;
  chain: "intact" | "broken" | null;
  signature: "attested" | "unchecked" | "unattested" | "forged" | null;
  ledger: string;
  page: string | null;
};
export type Listing = { folder: string; total: number; runs: Run[] };

/** `seatbelt report --json` and the usage on a run's page */
export type Usage = {
  runs: number;
  calls: number;
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  denials: number;
};
export type Fleet = {
  runs: number;
  by_principal: Record<string, Usage>;
  people: Record<string, string[]>;
  by_model: Record<string, Usage>;
  by_tool: Record<string, number>;
  failed: string[];
  incomplete: string[];
  unattested: string[];
  forged: string[];
  unsigned: string[];
  broken: string[];
  missing: string[];
};

/** `seatbelt verify --json` */
export type Verification = {
  ledger: string;
  ok: boolean;
  events: number;
  chain: "intact" | "broken";
  first_bad_seq: number | null;
  complete: boolean;
  signature: "attested" | "unchecked" | "unattested" | "forged" | null;
  reason: string | null;
};

/** `seatbelt reconstruct --json`: what a run's page shows; every string already printable */
export type Row = {
  seq: number;
  time: string;
  kind: string;
  css: string;
  actor: string;
  what: string;
  denied: boolean;
  failed: boolean;
  id: string;
  parent: string;
  hash: string;
  prev_hash: string;
  detail: string;
  cut: number;
  detail_end: string;
};
export type Page = {
  title: string;
  run_id: string;
  name: string;
  principal: string;
  client: string;
  agent: string;
  started: string;
  ended: string;
  duration: string;
  outcome: "ok" | "failed" | "incomplete";
  outcome_reason: string;
  events: number;
  complete: boolean;
  signature: string;
  signature_note: string;
  final_hash: string;
  ledger_sha256: string;
  ledger_file: string;
  verify_command: string;
  version: string;
  models: Record<string, Usage>;
  total: Usage;
  tools: Record<string, number>;
  refusals: { seq: number; rule: string; reason: string }[];
  failures: { seq: number; kind: string; error: string }[];
  rows: Row[];
};
