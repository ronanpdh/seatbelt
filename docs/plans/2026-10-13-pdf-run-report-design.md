# A run as a PDF: design

**Goal:** any run seatbelt recorded can be written as one PDF, to keep, print or send, from the CLI and from the desktop app. It says what the run's HTML page says, in a form that suits paper: the facts, what was checked and the summary, then each event on one line, with the events refused by policy or that failed given in full.

**Status:** implemented, unreleased, on a branch stacked on the desktop app's (PR 52). Facts come from the repository and the sources in the source map, read 2026-10-13, and from the checks below, run in this repository's development container (Linux, Python 3.12, Pango installed) the same day. Everything under "Design (ours)" and "Rejected (ours)" is our decision or the maintainer's.

## What exists

- **Each run's HTML page** ([design](2026-10-09-html-run-report-design.md)): `<run id>.html` beside the ledger, written as the run ends, or by `seatbelt reconstruct [run] --html [--out]`.
  - `html.build` reads the ledger once and refuses a broken chain, a forged signature, or no signature when a key is given. The page says what was checked when it was made.
  - Jinja2 escapes every value (`autoescape=True`), and every string goes through `printable`.
  - The page is written mode 0600, to a temporary file renamed into place.
  - `seatbelt erase` removes it with the ledger. `pack` takes only ledgers, signatures and `findings.json`.
- **The HTML design rejected PDF:** "It needs a rendering engine as a dependency. Left for later."
- **The desktop app's run view** opens the page in the system's browser (`open_page`). It finds a run's files by id, directly in the runs folder seatbelt names (`runs::file_of`).
- **CI** runs the unit tests on Ubuntu with `uv sync --locked`, which installs the `dev` group. The `lowest` workflow runs them with direct dependencies at their floors, and runs `pip-audit` over every extra at its floor.

## What the maintainer chose (2026-10-13)

- **One run,** not a report over an evidence pack.
- **An optional `pdf` extra** that renders with WeasyPrint, not printing from the browser and not a pure-Python PDF library.
- **The summary and one line per event,** with full detail only for refused and failed events.
- **Both the CLI and the desktop app.**

## WeasyPrint

- **Release:** 70.0, the latest on PyPI, uploaded 2026-09-08. It requires Python 3.10 or later.
- **Dependencies:** pydyf, cffi, tinyhtml5, tinycss2, cssselect2, Pyphen, Pillow and `fonttools[woff]`. The `woff` extra brings in `brotli` (D11).
- **No type information:** the package has no `py.typed`.
- **System libraries, per its install notes:**
  - Ubuntu (virtualenv, wheels): `libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0`;
  - macOS: `brew install weasyprint`;
  - Windows: Pango from MSYS2.
- **Its security notes** for untrusted input:
  - use "a custom URL fetcher" that "doesn't allow `file://` URLs or filters access depending on given paths";
  - "truncate and sanitize HTML and CSS input to avoid very long documents";
  - limit "rendering time and memory use".
- **URL fetchers:** a class inheriting `URLFetcher` with its own `fetch` can be given as `url_fetcher`. WeasyPrint catches every exception a fetch raises and goes on without the resource, "except when they inherit from `FatalURLFetchingError`", which "stops the rendering". `FatalURLFetchingError` is a `BaseException`.
- **Presentational hints** are off by default (`DEFAULT_OPTIONS`: `presentational_hints: False`).
- **Advisories (D1):** every release from 62.3 to 68.1 has at least three, and 69.0 two; 70.0 has none.
  - PYSEC-2026-3412: CSS injection from HTML attributes "when HTML presentational hints are enabled".
  - PYSEC-2026-3940: `write_pdf()`'s `stylesheets` and `xmp_metadata` options "ignore the document's `url_fetcher`".
  - CVE-2026-106443: image bytes "passed directly to Pillow's generic format dispatcher", which can hand them to Ghostscript.

## Design (ours)

1. **`seatbelt reconstruct [run] --pdf [--out run.pdf]`** makes the same checks as `--html`. It writes `<run id>.pdf` beside the ledger, where `erase` looks, or where `--out` says.
   - `--out` must end in `.pdf`.
   - `--html` and `--pdf` together are refused, as is `--out` without one of them.
   - Without the extra, or without Pango, it says how to get them, exits 1, and writes nothing.
2. **What the PDF shows.**
   - The page's header, its "What was checked" (saying "this PDF") and its summary. These come from template parts shared with the page (`templates/_run.html`), so the two cannot drift apart.
   - Every event on one line: sequence, time, kind, actor, and the one-line summary `reconstruct` prints.
   - Refused and failed events in full, as the page's detail shows them, up to 16 KiB (the first 4 KiB and the last 12 KiB). A refused or failed event's line links to its detail.
   - Running heads: "seatbelt run report" and the run's title; "A view, not evidence" and the page number.
3. **Long values cannot stall it (D6).** WeasyPrint lays out an unbroken run of text in time that grows much faster than its length, worst in grid, flex and auto-width table layouts.
   - The PDF's stylesheet uses only blocks and fixed-layout tables.
   - Every ledger value in a fact or a table cell is cut to its first 500 characters, with a note of how many more the ledger holds.
   - Every run of more than 80 characters with no space gets a zero-width space every 80 characters. A long value copied out of the PDF carries those characters.
4. **It loads nothing (D3).** Ledger text is escaped by the same template parts as the page. The PDF's own template refers to no URL. WeasyPrint is given a URL fetcher that refuses every URL, files and `data:` included, by raising `FatalURLFetchingError`, so a document that asked for anything is not made at all.
   - Against the advisories: presentational hints stay off, no `stylesheets` or `xmp_metadata` option is passed, and the documents hold no images.
5. **A view, not evidence,** as the page: made only from a ledger that verifies, and saying what was checked when it was made. Written mode 0600, to a temporary file renamed into place.
6. **On demand only.** A PDF takes seconds for a long run (D7), and the page is already the per-run view written at the end of each run.
7. **Erase and pack.** `erase` removes `<run id>.pdf` beside the ledger with its page, before the ledger, lists it, and counts it in `erasure.pages`. `pack` already takes only ledgers, signatures and findings.
8. **The desktop app.**
   - A run's view has "Open as PDF". The new `open_pdf` command runs `seatbelt reconstruct <ledger> --pdf`, finding the ledger by the run's id as `run_detail` does, then opens `<run id>.pdf` from the runs folder in the system's PDF viewer.
   - It is made again each time, so it says what the checks found then.
   - When seatbelt makes none, its own last words are shown under the buttons.
9. **Dependencies.** The extra is `pdf = ["weasyprint>=70.0"]`: the first release with no known advisory, as the dependency-floors rule asks. The `dev` group gets WeasyPrint and `pypdf>=6.20.1`, which the tests use to read PDFs back. CI installs WeasyPrint's Ubuntu packages before the unit tests, in `ci.yml` and `lowest.yml`.
10. **The gateway and brotli (D11).** With `brotli` installed, the gateway's HTTP client asks upstream for `br` too, as it asks only for what it can decode, and the gateway already drops `Content-Encoding` from what it relays. A gateway test that assumed no `br` now checks the rule instead: the client's own `Accept-Encoding`, never the caller's.

## Checks (2026-10-13)

| Ref | Check | Result |
|---|---|---|
| D1 | `pip-audit==2.10.1 -r <file> --no-deps` on `weasyprint==` 62.3, 63.1, 64.1, 65.0, 65.1, 66.0, 67.0, 68.0, 68.1, 69.0, 70.0, and on `pypdf==6.20.1` | 62.3 to 68.1: PYSEC-2026-3412, PYSEC-2026-3940 and CVE-2026-106443 among those reported. 69.0: PYSEC-2026-3940 and CVE-2026-106443, both fixed in 70.0. 70.0: "No known vulnerabilities found". pypdf 6.20.1: none |
| D2 | `uv lock --check` with uv 0.12.20 (CI's); `pip-audit` over `uv export --no-dev --all-extras` of the lock | the lock passes. The audit reports urllib3 2.7.0 (PYSEC-2026-4175, -4176 and -4177, fixed in 2.8.0), locked the same on `main`, through `requests` and the `openai-agents` extra: not from this change |
| D3 | WeasyPrint 70.0 with the refusing fetcher: an `<img>` with an `http:`, a `file:` and a `data:` URL; `@import`; a CSS `background` URL; a `<link>` stylesheet; `@font-face`. The PDF template rendered with a fetcher that counts | each stopped the render with `FatalURLFetchingError`; the template asked for no URL |
| D4 | A reference run's HTML page rendered before and after the template parts moved to `_run.html` | byte-identical |
| D5 | The reference run (a `<script>` and a right-to-left override in a prompt, an `<img onerror>` in an error, a refusal, a failed tool call) as a PDF | 3 pages, mode 0600; the markup shown as text and the override as `‮`; the refusal and the failed call in full; PDF title, subject, creator and producer set |
| D6 | Layout time of one unbroken 3,000-character word in each construct; then a whole run with one unbroken value everywhere | a paragraph 0.21 s; `pre` 0.12 s; a fixed-layout table cell 0.38 s; an auto-width table cell 20.87 s; inside a grid 87.53 s; inside flexbox 454.97 s. In `pre`: 16,000 characters 1.61 s and 64,000 25.17 s, or 0.18 s and 1.07 s with a zero-width space every 80. A run with a 2,000-character value took 36.6 s before the change, and a test run with one of 49,152 had not finished after ten minutes; after it, 2,000 took 1.31 s, 49,152 0.95 s, and 200,000 0.82 s |
| D7 | The synthetic sessions of the HTML design's check C7, as PDFs | 150 turns (a 21.8 MB ledger): 11.3 s before fixed table layout (8.2 s of it layout), 3.9 to 4.4 s after; 20 pages, 0.1 MB. 400 turns (150.1 MB): 31.1 s before, 13.5 to 13.8 s after; 52 pages, 0.2 MB, a peak of 715 MB of memory |
| D8 | The wheel built with uv 0.12.20; installed without the extra in a new virtualenv | it holds `templates/_run.html`, `run.html` and `run_pdf.html`, and `Requires-Dist: weasyprint>=70.0 ; extra == 'pdf'`. Installed without it, `--pdf` printed the install note, exited 1 and wrote nothing; `--html` still worked |
| D9 | `pytest -m "not integration"` as root; ruff; pyright (strict) | 779 passed; 7 failed, the same 7 that fail as root on `main` (the sandbox refuses root; root ignores `chmod 500`). `tests/unit/test_pdf_report.py` has 19 tests. ruff and pyright clean |
| D10 | The desktop app under Xvfb, as for the desktop design's L checks, with a stand-in `xdg-open` that logs what it is given: a run's view, "Open as PDF"; then the same with `seatbelt` installed without the extra | the button showed "Making the PDF…", then the stand-in was given `<runs folder>/<run id>.pdf`, mode 0600, 22 pages, attested, with the denied Bash call's result in full. Without the extra, nothing was opened and the view showed "seatbelt made no PDF: a PDF needs the pdf extra: …" |
| D11 | `uv tree --invert --package brotli`; `httpx2.AsyncClient().headers` | brotli comes from `fonttools[woff]`, through WeasyPrint; httpx2's default `Accept-Encoding` is then `gzip, deflate, br` |
| D12 | `cargo test`, clippy (`-D warnings`), `tsc` | 60 tests pass, adding a run's PDF found by its id and seatbelt's last words on one line; clippy and tsc clean |

## Rejected (ours)

- **Printing the page from the browser:** the maintainer's choice. It is a manual step, not a command.
- **A pure-Python PDF library,** such as fpdf2: a second layout to keep in step with the page's.
- **A headless browser,** through Playwright: a browser download as a dependency.
- **Writing a PDF as each run ends:** seconds on the CLI's way out for a long run (D7), for a view the page already gives.
- **Every event in full:** the maintainer's choice. A long run's PDF would run to hundreds of pages.
- **Refusing a document's requests one by one,** WeasyPrint's default when a fetch fails: the render would go on without the resource. Stopping it is the stronger answer, since the template asks for none.

## Not done

- A report over an evidence pack, which `docs/BUILD.md` lists as planned.
- PDF/A and tagged (accessible) PDF: WeasyPrint has options for both, not tried.
- Pango on macOS and Windows, and "Open as PDF" outside Linux.
- Fonts: WeasyPrint uses the system's. A script with no installed font was not tried.
- A time or memory limit on rendering, which WeasyPrint's security notes suggest. The length limits above are what bound it.
- A PDF written elsewhere with `--out` is the user's own copy: `erase` does not know about it, as for the page.

## Source map

All pages read 2026-10-13.

| Section | Claim | Source |
|---|---|---|
| What exists | the page, its checks, escaping, mode and erase/pack handling | `src/seatbelt/report/html.py` (module docstring, `build`, `template`, `write_page`); `src/seatbelt/erase.py` (`_target`, `erase`); `src/seatbelt/report/pack.py` (`_MEMBER`) |
| What exists | the HTML design rejected PDF | `docs/plans/2026-10-09-html-run-report-design.md` ("Rejected (ours)"): "**PDF.** It needs a rendering engine as a dependency. Left for later." |
| What exists | the desktop run view opens the page; files by id | `desktop/src-tauri/src/lib.rs` (`open_page`); `desktop/src-tauri/src/runs.rs` (`file_of`) |
| What exists | CI and the lowest workflow | `.github/workflows/ci.yml` (`uv sync --locked`, `pytest -m "not integration"`); `.github/workflows/lowest.yml` (`--resolution lowest-direct`, `pip-audit` over `--all-extras`) |
| What the maintainer chose | scope, rendering, events, where | the maintainer's answers in this session, 2026-10-13 |
| WeasyPrint | 70.0 latest, uploaded 2026-09-08; Python ≥3.10; its dependencies | https://pypi.org/pypi/weasyprint/json (`info.version`, `info.requires_python`, `info.requires_dist`, release upload times) |
| WeasyPrint | system packages per platform | https://doc.courtbouillon.org/weasyprint/stable/first_steps.html: Ubuntu "apt install python3-pip libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0"; macOS "brew install weasyprint"; Windows "pacman -S mingw-w64-ucrt-x86_64-pango" |
| WeasyPrint | its security notes | the same page ("Security"): "a custom URL fetcher" that "doesn't allow `file://` URLs or filters access depending on given paths"; "truncate and sanitize HTML and CSS input to avoid very long documents"; limit "rendering time and memory use of your process" |
| WeasyPrint | a fetcher subclass; exceptions caught except `FatalURLFetchingError`, which stops rendering; a `BaseException`; no `py.typed`; presentational hints off by default | WeasyPrint 70.0's installed source: `weasyprint/urls.py` (`URLFetcher` docstring: "Another class inheriting from this class, with a ``fetch`` method that has a compatible signature, can be given as the ``url_fetcher`` argument"; `fetch` docstring: "All exceptions are catched internally by WeasyPrint, except when they inherit from :obj:`FatalURLFetchingError`"; `fetch()`: "Fatal errors must raise a ``FatalURLFetchingError`` that stops the rendering"; `class FatalURLFetchingError(BaseException)`); `weasyprint.DEFAULT_OPTIONS` (`'presentational_hints': False`); no `py.typed` in the package |
| WeasyPrint | the advisories and what each concerns | `pip-audit==2.10.1 --desc --format json` on `weasyprint==68.1`: PYSEC-2026-3412 ("when HTML presentational hints are enabled"), PYSEC-2026-3940 ("Two `write_pdf()` channels ignore the document's `url_fetcher`": `xmp_metadata` and `stylesheets`), CVE-2026-106443 ("passes fetched image bytes directly to Pillow's generic format dispatcher"); D1 |
| Design | brotli from `fonttools[woff]`; httpx2's default encodings | D11 |
| Design | the gateway drops the caller's `Accept-Encoding` and the upstream's `Content-Encoding` | `src/seatbelt/gateway/app.py` (`_STRIP_REQUEST`, with the comment "httpx2 decodes the body, so let it ask only for encodings it can decode"; `_STRIP_RESPONSE`) |
| Design | the dependency-floors rule | `CHANGELOG.md` (0.5.2): "Dependency floors are the lowest releases the tests pass with, and never below the first release from which no later one has a known advisory." |
| Checks | D1 to D12 | run in this repository's development container on 2026-10-13. D7 uses the generator of the HTML design's C7 |
