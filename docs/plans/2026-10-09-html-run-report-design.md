# An HTML report for each run: design

**Goal:** every run seatbelt records gets one HTML file beside its ledger, written automatically when the ledger is closed and signed. A person can open it in a browser, keep it, or send it on. Any ledger can also be rendered on demand.

**Status:** a design for review. Facts come from the repository and the sources in the source map, read 2026-10-09. Everything under "Design (ours)" and "Rejected (ours)" is our decision.

## What exists

- `seatbelt reconstruct` prints a run as a terminal table: sequence number, time, kind, actor, a one-line summary cut to 80 characters, and the first 10 characters of each event's hash. It refuses a ledger whose chain is broken or whose signature is forged.
- `seatbelt report` sums usage by person, model and tool, and lists failed, incomplete, unattested, forged, unsigned, broken and missing runs. It counts only ledgers whose chain verifies.
- Every string from a ledger goes through `printable` before it is shown. `printable` makes control characters visible, including the bidirectional overrides that can reorder what a reader sees.
- The local recorder is given a callback that runs each time it closes and signs a ledger. Today that callback collects the ledger's path and hands it to the sink.
- A ledger file is created with mode 0600.
- What reads the runs folder:
  - `pack` collects `*.jsonl` files only;
  - `erase` removes a person's ledger, its signature and its ship mark;
  - the sink uploads a ledger and its signature only.
- `STANDARDS.md` names Jinja2 for report templates. Jinja2 is not a dependency yet: it is in neither `pyproject.toml` nor `uv.lock`.
- `docs/BUILD.md` lists "a rendered HTML or PDF evidence report" as planned and not built. ADR 0004 calls rendered output "a later layer over the same pack".

## Design (ours)

1. **One file per run, beside its ledger.** The file is `<run id>.html` in the runs folder.
   - It is written when the ledger is closed and signed, from the same callback the sink uses. This covers `seatbelt run` and the desktop recorder ([desktop design](2026-10-09-desktop-recorder-design.md)).
   - `html = false` in `~/.config/seatbelt/config.toml` turns it off.
   - The org gateway does not write pages in this version.
2. **On demand.** `seatbelt reconstruct [run] --html [path]` writes the page for any ledger and prints where.
   - The default path is `<run id>.html` in the current folder.
   - It makes the same checks as `reconstruct`: a broken or forged ledger gets no page, and the command exits 1.
3. **A view, not evidence.**
   - The top of the page states the verdict at the time it was rendered:
     - chain intact or not;
     - complete or not;
     - attested, unchecked or unattested;
     - the event count, the final hash and the ledger's SHA-256;
     - the command that checks the ledger again.
   - The page is not signed. Like any file, it can be edited. The ledger and its signature are what prove anything, and the page says so.
   - A ledger that fails verification is never rendered, even in part. So a page can never show a tampered run as clean.
4. **What it shows.**
   - **Header:**
     - the run's name and id;
     - who (`principal.id`);
     - the client (its user agent, when recorded);
     - start, end and duration;
     - the outcome: ok, failed with its reason, or incomplete.
   - **Summary:**
     - per model: calls, input tokens, cache read, cache write and output tokens;
     - tool calls by name;
     - policy checks that refused, with their reasons;
     - model errors.
   - **Timeline:** one row per event, with:
     - sequence number;
     - local time with its UTC offset;
     - kind and actor;
     - the one-line summary `reconstruct` prints.

     Each row expands, with `<details>`, to show the event's full attributes as formatted JSON, its id, parent id, hash and previous hash.
   - **Large content:** each event's expanded JSON is cut at 64 KiB. The page says how much was left out and that the ledger holds all of it.
5. **Safe to open.** Everything from the ledger is untrusted: prompts, model output and tool results can all contain HTML.
   - Templates are Jinja2 with autoescaping turned on, so every value is escaped unless a template says otherwise. No template says otherwise.
   - Every string from the ledger also goes through `printable`, as in the terminal, so bidirectional overrides and control characters are visible.
   - A Content-Security-Policy `<meta>` tag allows no scripts, no network and inline styles only, as a second line of defence if a value ever escapes the template.
   - The page has no JavaScript, web fonts or images, and fetches nothing. Its styles are inline. It works offline and sends no request when opened.
6. **Same protection as the ledger.**
   - Mode 0600.
   - Written to a temporary file and renamed into place, so nobody reads half a page.
   - It holds the same text as the ledger, which was redacted before it was first written.
7. **Kept with its ledger.**
   - `seatbelt erase` removes `<run id>.html` along with the ledger and its signature, and lists it among what it will remove. A page is a copy of the person's data.
   - `pack` and the sink leave it out. It can be rebuilt from the ledger, and a pack's reader checks ledgers, not pages.
   - `report`, `runs` and `verify` read `*.jsonl` and are unaffected.
8. **Code.**
   - `seatbelt/report/html.py` holds `render(ledger, pubkey) -> str` and `write(ledger, pubkey, out) -> Path`.
   - The template is `seatbelt/report/templates/run.html.j2`, shipped in the wheel.
   - The one-line summary comes from the function `reconstruct` uses (`timeline._describe`, made public), so the terminal and the page say the same thing.
   - The sums come from `fleet`'s `Usage`, through a function that sums one ledger's events, so the page and `seatbelt report` count the same way.
   - New dependency: Jinja2, and MarkupSafe with it. The floor follows the dependency-floors rule: the lowest release the tests pass with, and no release from the floor up with a known advisory.
9. **A page that fails does not fail the run.** If a page cannot be written (disk full, a template error), seatbelt logs a warning and the run still ends signed. `seatbelt reconstruct <run> --html` makes it again.

## Rejected (ours)

- **PDF.** It needs a rendering engine as a dependency. Left for later.
- **Signing the page.** A signature over a view adds nothing the ledger's signature does not, and invites people to treat the page as evidence.
- **JavaScript for search and filters.** A script in a page built from untrusted text is a risk this version does not need.
- **Escaping by hand with the standard library.** Every value would have to be escaped at its call site, and one missed call would let a prompt inject markup. Jinja2 escapes by default.

## Tests

- A prompt, a tool result and a model reply containing `<script>`, an `<img onerror>`, a closing `</details>` and a `javascript:` URL all render as text. This is a golden-file test.
- The page loads nothing: no `<script>`, `<link>`, `<img>`, `<iframe>`, `@import` or `url(`, and no `href` other than an in-page `#` anchor.
- A property test: random strings in any attribute never produce a `<` in the page outside the template's own markup.
- A ledger with a broken chain, or a forged signature, gets no page, and the command exits 1.
- An incomplete ledger gets a page that says it is incomplete.
- The page's mode is 0600, and an interrupted write leaves no partial file.
- `erase` removes the page; `pack` and the sink leave it out.
- A run whose page fails to render still ends signed.

## Not done

- Pages on the org gateway, which holds everyone's ledgers. That needs its own decision about who may read them.
- An index page for a runs folder. The desktop app lists runs and opens their pages.
- PDF.
- Recording the page's hash in an erasure record. The record names the ledgers it removed. The page is derived from one of them.

## Source map

| Section | Claim | Source |
|---|---|---|
| What exists | `reconstruct` prints seq, time, kind, actor, an 80-character summary and a 10-character hash | `src/seatbelt/report/timeline.py` (`_summary`, `timeline`) |
| What exists | `reconstruct` refuses a broken chain or a forged signature | `src/seatbelt/cli.py` (`_check`, `reconstruct`) |
| What exists | `report` sums by person, model and tool, lists failed, incomplete, unattested, forged, unsigned, broken, missing; counts only verifying chains | `src/seatbelt/report/fleet.py` (module docstring, `Fleet`, `fleet`) |
| What exists | `printable` makes control characters and bidirectional overrides visible | `src/seatbelt/terminal.py` (`_CODES`, `printable`) |
| What exists | the local recorder's close callback collects paths and ships them | `src/seatbelt/gateway/local.py` (`closed`), `src/seatbelt/gateway/sessions.py` (`_close`, `on_close`) |
| What exists | ledgers are created with mode 0600 | `src/seatbelt/ledger/store.py` (`self.path.touch(mode=0o600)`) |
| What exists | `pack` collects `*.jsonl` only | `src/seatbelt/report/pack.py` (`runs_dir.glob("*.jsonl")`) |
| What exists | `erase` removes ledger, signature and ship mark | `src/seatbelt/erase.py` (`t.ledger.unlink`, `t.sidecar.unlink`, `t.mark.unlink`) |
| What exists | the sink uploads a ledger and its signature only | `src/seatbelt/gateway/sink.py` (`_upload`) |
| What exists | `STANDARDS.md` names Jinja2 for report templates; not in `pyproject.toml` or `uv.lock` | `STANDARDS.md` (Tooling table); `pyproject.toml`; `uv.lock` (no `jinja2` entry) |
| What exists | BUILD.md: HTML or PDF report planned, not built; ADR 0004: "a later layer over the same pack" | `docs/BUILD.md` ("Planned, not built"); `docs/adr/0004-evidence-pack.md` (Rejected) |
| Design 7 | `report`, `runs`, `verify` read `*.jsonl` | `src/seatbelt/report/fleet.py` (`d.glob("*.jsonl")`), `src/seatbelt/cli.py` (`runs`, `_resolve`) |
| Design 8 | dependency-floors rule | `docs/plans/2026-09-29-dependency-floors-design.md` ("Floors") |
