# An HTML report for each run: design

**Goal:** every run seatbelt records gets one HTML file beside its ledger, written automatically when the ledger is closed and signed. A person can open it in a browser, keep it, or send it on. Any ledger can also be rendered on demand.

**Status:** implemented, unreleased. Facts come from the repository and the sources in the source map, read 2026-10-09. A second reader checked every claim against its source the same day and corrected three. Everything under "Design (ours)" and "Rejected (ours)" is our decision. Where the build differs from this design, it says so under "As built".

## What exists

- `seatbelt reconstruct` prints a run as a terminal table: sequence number, time, kind, actor, a one-line summary cut to 80 characters, and the first 10 characters of each event's hash. It refuses a ledger whose chain is broken or whose signature is forged, and, when a key is given, one that is unsigned.
- `seatbelt report` sums usage by person, model and tool, and lists failed, incomplete, unattested, forged, unsigned, broken and missing runs. It counts only ledgers whose chain verifies.
- Every string from a ledger goes through `printable` before it is shown. `printable` makes control characters visible, including the bidirectional overrides that can reorder what a reader sees.
- The local recorder is given a callback that runs each time it closes and signs a ledger. Today that callback collects the ledger's path and hands it to the sink.
- A ledger a killed run left open is closed and signed later, by the next run's start-up tidy. That path does not call the callback; the sink finds such ledgers by scanning the folder.
- A ledger file is created with mode 0600.
- What reads the runs folder:
  - `pack` collects ledgers, their signatures and `findings.json`, and no other file;
  - `erase` removes a person's ledger, its signature and its ship mark;
  - the sink uploads a ledger and its signature only.
- `STANDARDS.md` names Jinja2 for report templates. Jinja2 is not a dependency yet: it is in neither `pyproject.toml` nor `uv.lock`.
- `docs/BUILD.md` lists "a rendered HTML or PDF evidence report" as planned and not built. ADR 0004 calls rendered output "a later layer over the same pack".

## Design (ours)

1. **One file per run, beside its ledger.** The file is `<run id>.html` in the runs folder.
   - It is written when the ledger is closed and signed, from the same callback the sink uses. This covers `seatbelt run` and the desktop recorder ([desktop design](2026-10-09-desktop-recorder-design.md)).
   - The start-up tidy also writes a page for each killed run's ledger it closes and signs, so those runs get one too.
   - `html = false` in `~/.config/seatbelt/config.toml` turns it off.
   - The org gateway does not write pages in this version.
2. **On demand.** `seatbelt reconstruct [run] --html [--out path]` writes the page for any ledger and prints where.
   - The default path is `<run id>.html` in the current folder.
   - `--out` must end in `.html` (or `.htm`), so a slip cannot write a page over a ledger.
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
     - the one-line summary `reconstruct` prints, cut at 80 characters as there.

     Each row expands, with `<details>`, to show the event's full attributes as formatted JSON, its id, parent id, hash and previous hash.
   - **Large content:** each event's expanded JSON is cut at 64 KiB. The page says how much was left out and that the ledger holds all of it. (As built: the start and the end are kept; see below.)
5. **Safe to open.** Everything from the ledger is untrusted: prompts, model output and tool results can all contain HTML.
   - Templates are Jinja2 with `autoescape=True`, so every value is escaped unless a template says otherwise. No template says otherwise. Autoescaping is off in a plain Jinja2 `Environment`, and `select_autoescape` chooses by the template's file extension with a default of off. So seatbelt sets `True` outright, and a test fails if a value reaches the page unescaped.
   - Every string from the ledger also goes through `printable`, as in the terminal, so bidirectional overrides and control characters are visible.
   - A Content-Security-Policy `<meta>` tag is the first child of `<head>`. It allows no scripts, no network and inline styles only, as a second line of defence if a value ever escapes the template. It comes first because a policy in a `<meta>` tag does not apply to content before it.
   - Whether browsers enforce a CSP on a page opened from disk (`file://`) is not stated in the CSP or HTML specs, or on MDN. Chromium 141 does (check C1). Firefox and Safari were not available to test. Autoescaping is the defence that must hold either way.
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
   - The row's one-line summary comes from the function `reconstruct` uses (`timeline._summary`, made public), so the terminal and the page say the same thing. `_summary` is `_describe` passed through `printable`, with whitespace collapsed and cut at 80 characters.
   - The sums come from `fleet`'s `Usage`, through a function that sums one ledger's events, so the page and `seatbelt report` count the same way.
   - New dependency: Jinja2, and MarkupSafe with it. The floor follows the dependency-floors rule: the lowest release the tests pass with, and no release from the floor up with a known advisory.
9. **A page that fails does not fail the run.** If a page cannot be written (disk full, a template error), seatbelt logs a warning and the run still ends signed. `seatbelt reconstruct <run> --html` makes it again.

## As built

- **`--html` is a flag and `--out` names the file,** not `--html [path]`: Typer has no option that takes a value only sometimes.
- **The page lives in `seatbelt/report/html.py` (`build`, `render`, `write_page`) and `seatbelt/report/templates/run.html`.** `page_path` is in `seatbelt/report/__init__.py`, because `erase` needs it and `report.html` imports `report.fleet`, which imports `erase`.
- **Shared with the terminal and `report`:** `timeline.summary` (was `_summary`) for the one-line text, and `fleet.counted`, `fleet.response_model` and `fleet.count_response` for usage.
- **`seatbelt run` prints the page's path** (`page:`) under the ledger's (`file:`), quoted for a shell. Pages that the start-up tidy writes for killed runs are not listed: they are not this run's.
- **The erasure record counts pages** (`erasure.pages`), and `erase` removes a page before its ledger, so an interrupted erase finds it again by the ledger.
- **A long event keeps its start and its end.** The design cut at 64 KiB from the start. A model request's messages run oldest first, so in a long session that showed only the opening of the conversation, never the turn the request was for. An event over 64 KiB now shows its first 8 KiB, how many bytes from the middle are left out, and its last 56 KiB.
- **A long event is shown as compact JSON.** Indented JSON makes Python's `json` use its pure-Python encoder; events that fit in 64 KiB are still indented.
- **Jinja2's floor is 3.1.6.** `pip-audit` 2.10.1 on 2026-10-09 reported advisories against 3.1.4 and 3.1.5, all fixed by 3.1.6, and none against 3.1.6 (C2).

## Checks

| Ref | Check | Result |
|---|---|---|
| C1 | A rendered page with `<script>document.title="SCRIPT RAN"</script><img src="https://example.com/x.png">` put right after `<meta charset>` (so after the CSP tag), opened from `file://` in Chromium 141.0.7390.37 through Playwright 1.56.1; and the same page with the CSP tag removed | With the CSP: the title unchanged; the console "Refused to execute inline script because it violates the following Content Security Policy directive"; the image request failed with reason `csp`, before the network. Without it: the title "SCRIPT RAN", and the image request went out (it failed later, `net::ERR_BLOCKED_BY_ORB`). So Chromium enforces the page's CSP on `file://` |
| C2 | `pip-audit==2.10.1 -r <file> --no-deps` on `jinja2==3.1.4`, `3.1.5` and `3.1.6` | 3.1.4: PYSEC-2026-1471 (fixed in 3.1.6), PYSEC-2026-1472 and PYSEC-2026-1475 (fixed in 3.1.5). 3.1.5: PYSEC-2026-1471. 3.1.6: "No known vulnerabilities found" |
| C3 | A signed ledger whose prompt and tool result hold `<script>`, `<img src=x onerror=…>` and `</details>`, and whose prompt holds a right-to-left override and an ESC sequence, rendered and opened from `file://` in Chromium 141 (light and dark) | no request; title unchanged; the payloads shown as text, the override and ESC as `\u202e` and `\u001b`. `</pre>`, `javascript:` links and `<style>` are covered by the unit tests only |
| C4 | `uv run pytest -m "not integration"` as root | 747 passed; 7 failed, the same 7 that fail as root before this change (the sandbox refuses root; root ignores `chmod 500`) |
| C5 | The same as an unprivileged user, with the container's root-only CA bundle variables unset | 754 passed |
| C7 | Synthetic signed sessions in which each request carries the whole history so far (about 1.8 KB more per turn), a tool call per turn; `write_page` timed on this container | 150 turns: a 21.8 MB ledger, 0.6 s, a 10.2 MB page. 400 turns: 150 MB, 4.1 s, 29.1 MB. At 400 turns: reading the ledger 0.42 s, the chain 0.54 s, the signature 1.14 s, the rows 0.85 s, the template 0.1 s |
| C6 | `uv build --wheel` (uv 0.12.20) | the wheel holds `seatbelt/report/templates/run.html` and `Requires-Dist: jinja2>=3.1.6` |

## Rejected (ours)

- **PDF.** It needs a rendering engine as a dependency. Left for later.
- **Signing the page.** A signature over a view adds nothing the ledger's signature does not, and invites people to treat the page as evidence.
- **JavaScript for search and filters.** A script in a page built from untrusted text is a risk this version does not need.
- **Escaping by hand with the standard library.** Every value would have to be escaped at its call site, and one missed call would let a prompt inject markup. Jinja2 escapes by default.

## Tests

In `tests/unit/test_html_report.py` unless named.

- A prompt, tool arguments and result, a model reply, the client and a refusal reason holding `<script>`, an `<img onerror>`, closing `</details>` and `</pre>`, a `javascript:` link and a `<style>` all render as text, and the page keeps the template's structure.
- A property test (Hypothesis, 60 examples): for any text in those places, the page has the same elements and attributes, in the same order, as for plain text.
- Bidirectional overrides, ESC and C1 controls are shown as escapes, never written raw.
- The page loads nothing: the CSP tag is the first element in `<head>` with `default-src 'none'` and no script source; there is no `<script>`, `<link>`, `<img>`, `<iframe>`, `<object>`, `<embed>`, `<base>` or `<form>`, no `src` or `on…` attribute, no `@import` or `url(`, and no `href` but in-page `#e<seq>` anchors.
- In a browser, a `<script>` deliberately put after the CSP tag does not run: by hand in Chromium (check C1), not in CI.
- A broken chain or a forged signature gets no page; an unsigned ledger is shown as unattested, and refused when a key is given; an open ledger is shown as incomplete; a failed run shows its error.
- A long event shows its start and end, and how much of its middle is left out.
- The page is mode 0600, replaces an older page whole, and an interrupted write leaves no file behind.
- `reconstruct --html` writes the page, `--out` names it, an `--out` not ending in `.html` and an `--out` without `--html` are refused, and a tampered ledger gets no page.
- `pack` leaves pages out. (The sink uploads only a ledger and its signature by construction; no test was added.)
- `test_local.py`: a local run writes its page, mode 0600 and attested, and prints its path; `html = false` writes none; a page that cannot be written leaves the run signed with the CLI's exit code; a killed run's ledger gets its page from the next run.
- `test_launcher.py`: `html` in the config is true or false.
- `test_erase.py`: `erase` removes the person's pages and no one else's, and counts them in the record.

## Not done

- Writing the page off the CLI's exit path. A very long run waits a few seconds for it (check C7).
- Pages written elsewhere with `--out` are the user's own copies; `erase` does not know about them.
- The `file://` CSP check in Firefox and Safari.
- Pages on the org gateway, which holds everyone's ledgers. That needs its own decision about who may read them.
- An index page for a runs folder (`seatbelt runs` lists them).
- PDF.
- Recording the page's hash in an erasure record. The record names the ledgers it removed. The page is derived from one of them.

## Source map

| Section | Claim | Source |
|---|---|---|
| What exists | `reconstruct` prints seq, time, kind, actor, an 80-character summary and a 10-character hash | `src/seatbelt/report/timeline.py` (`_summary`, `timeline`) |
| What exists | `reconstruct` refuses a broken chain, a forged signature, or with a key an unsigned ledger | `src/seatbelt/cli.py` (`_check`, `reconstruct`) |
| What exists | `report` sums by person, model and tool, lists failed, incomplete, unattested, forged, unsigned, broken, missing; counts only verifying chains | `src/seatbelt/report/fleet.py` (module docstring, `Fleet`, `fleet`) |
| What exists | `printable` makes control characters and bidirectional overrides visible | `src/seatbelt/terminal.py` (`_CODES`, `printable`) |
| What exists | the local recorder's close callback collects paths and ships them | `src/seatbelt/gateway/local.py` (`closed`), `src/seatbelt/gateway/sessions.py` (`_close`, `on_close`) |
| What exists | ledgers are created with mode 0600 | `src/seatbelt/ledger/store.py` (`self.path.touch(mode=0o600)`) |
| What exists | `pack` collects ledgers, their signatures and `findings.json` only | `src/seatbelt/report/pack.py` (`runs_dir.glob("*.jsonl")`, the sidecar for each, `findings.json`) |
| What exists | the start-up tidy closes killed runs' ledgers without the close callback; the sink catches up by scanning | `src/seatbelt/gateway/sessions.py` (`close_open_chains`); `src/seatbelt/gateway/local.py` (`tidy`); `src/seatbelt/gateway/sink.py` (`catch_up`) |
| What exists | `erase` removes ledger, signature and ship mark | `src/seatbelt/erase.py` (`t.ledger.unlink`, `t.sidecar.unlink`, `t.mark.unlink`) |
| What exists | the sink uploads a ledger and its signature only | `src/seatbelt/gateway/sink.py` (`_upload`) |
| What exists | `STANDARDS.md` names Jinja2 for report templates; not in `pyproject.toml` or `uv.lock` | `STANDARDS.md` (Tooling table); `pyproject.toml`; `uv.lock` (no `jinja2` entry) |
| What exists | BUILD.md: HTML or PDF report planned, not built; ADR 0004: "a later layer over the same pack" | `docs/BUILD.md` ("Planned, not built"); `docs/adr/0004-evidence-pack.md` (Rejected) |
| Design 4, 8 | `_summary` is `_describe` through `printable`, whitespace collapsed, cut at 80 | `src/seatbelt/report/timeline.py` (`_summary`) |
| Design 7 | `report`, `runs`, `verify` read `*.jsonl` | `src/seatbelt/report/fleet.py` (`d.glob("*.jsonl")`), `src/seatbelt/cli.py` (`runs`, `_resolve`) |
| Design 5 | Jinja2 autoescaping is off in a plain `Environment`; `autoescape=True` turns it on | https://jinja.palletsprojects.com/en/stable/api/, read 2026-10-09: "autoescaping is not yet enabled by default"; "autoescape: If set to True the XML/HTML autoescaping feature is enabled by default." |
| Design 5 | `select_autoescape` chooses by file extension, default off | same page: signature `select_autoescape(enabled_extensions=('html','htm','xml'), disabled_extensions=(), default_for_string=True, default=False)`; "If nothing matches then the initial value of autoescaping is set to the value of default." |
| Design 5 | a CSP may be delivered in a `<meta>` tag, and does not apply to content before it | https://www.w3.org/TR/CSP3/, read 2026-10-09: "A Document may deliver a policy via one or more HTML meta elements"; "policies in meta elements are not applied to content which precedes them." |
| Design 5 | whether a CSP applies to `file://` pages: not stated | CSP3, the HTML standard (https://html.spec.whatwg.org/multipage/semantics.html) and MDN (https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy), read 2026-10-09: no statement found |
| Design 4 | `<details>` opens and closes without script | https://html.spec.whatwg.org/multipage/interactive-elements.html, read 2026-10-09: "The activation behavior of summary elements is to run the following steps: … If the open attribute is present on parent, then remove it. Otherwise, set parent's open attribute" |
| Design 8 | Jinja2's latest release is 3.1.6 and needs Python 3.7 or newer; seatbelt needs 3.12 | https://pypi.org/pypi/Jinja2/json, read 2026-10-09: `"version": "3.1.6"`, `"requires_python": ">=3.7"`; `STANDARDS.md` (Python 3.12 or newer) |
| Design 8 | dependency-floors rule | `docs/plans/2026-09-29-dependency-floors-design.md` ("Floors") |
