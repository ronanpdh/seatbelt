"""A run as a PDF: the HTML page's facts, checks and summary, then one line per event, with
what was recorded, up to 16 KiB (its start and end beyond that), only for the events refused by
policy or that failed.

Like the page, the PDF is a view, not evidence: it is made only from a ledger that verifies, and
says what was checked when it was made. It is rendered by WeasyPrint, from the optional `pdf`
extra, from a template that shares the page's header, checks and summary, escaped as the page
is. The template refers to nothing outside itself, and the renderer is given a URL fetcher that
refuses every URL and stops the render: a value that ever escaped the template still loads
nothing. Design: docs/plans/2026-10-13-pdf-run-report-design.md.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import io
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from seatbelt.report import pdf_path
from seatbelt.report.html import Page, PageError, build, template

PDF_DETAIL_LIMIT = 16 * 1024  # bytes of a refused or failed event's detail; the ledger has all
PDF_DETAIL_HEAD = 4 * 1024
# WeasyPrint lays out an unbroken run of text in time that grows faster than its length, worse
# in an auto-width table cell and far worse in grid and flex (check D6 in the design): one value
# of 64,000 characters in a `pre` took 25 seconds. So a value from the ledger in a fact or a
# table cell is cut to VALUE_LIMIT characters, and every run of text longer than BREAK_EVERY in
# a value from the ledger gets a zero-width space every BREAK_EVERY characters.
VALUE_LIMIT = 500
BREAK_EVERY = 80
TEMPLATE = "run_pdf.html"
_LONG_RUN = re.compile(rf"\S{{{BREAK_EVERY + 1},}}")
INSTALL = (
    "a PDF needs the pdf extra: pip install 'seatbelt-ai[pdf]' (or uv tool install "
    "'seatbelt-ai[pdf]'), and the Pango library it uses: "
    "https://doc.courtbouillon.org/weasyprint/stable/first_steps.html"
)


# Where Homebrew (Apple silicon, then Intel) and MacPorts put Pango and the libraries it uses
MAC_LIBRARY_DIRS = ("/opt/homebrew/lib", "/usr/local/lib", "/opt/local/lib")
# where macOS looks when DYLD_FALLBACK_LIBRARY_PATH is unset; setting it replaces these
_MAC_DEFAULT_FALLBACK = ("~/lib", "/usr/local/lib", "/lib", "/usr/lib")


class PdfUnavailable(Exception):
    """WeasyPrint is not installed, or cannot load the system libraries it needs."""


def mac_library_path(env: Mapping[str, str], is_dir: Callable[[str], bool]) -> str | None:
    """`DYLD_FALLBACK_LIBRARY_PATH` with the folders Homebrew and MacPorts install into added
    (those that exist), or None when it has them all. On macOS, WeasyPrint names Pango and the
    libraries it uses by short names that the system looks for in none of those folders; its
    troubleshooting notes say to set this variable. cffi, which loads them, falls back to
    Python's own lookup, which reads the variable when it is called: so setting it in this
    process is enough, and it reaches a seatbelt the desktop app starts, which a variable set
    in a shell does not."""
    current = [p for p in env.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":") if p]
    base = current or [os.path.expanduser(p) for p in _MAC_DEFAULT_FALLBACK]
    prefix = env.get("HOMEBREW_PREFIX")
    wanted = [f"{prefix}/lib"] if prefix else []
    added = [d for d in dict.fromkeys([*wanted, *MAC_LIBRARY_DIRS]) if d not in base]
    added = [d for d in added if is_dir(d)]
    return ":".join([*base, *added]) if added else None


def _weasyprint() -> Any:  # Any: WeasyPrint has no type information
    if sys.platform == "darwin":
        path = mac_library_path(os.environ, os.path.isdir)
        if path is not None:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = path
    try:
        # WeasyPrint prints its own notice when a library is missing; the error below says it
        with contextlib.redirect_stdout(io.StringIO()):
            return importlib.import_module("weasyprint")
    except ImportError as exc:
        raise PdfUnavailable(INSTALL) from exc
    except OSError as exc:  # installed, but Pango or a library it needs cannot be loaded
        raise PdfUnavailable(
            f"WeasyPrint cannot load a library it needs ({exc}); {INSTALL}"
        ) from exc


def weasyprint_version() -> str:
    """The WeasyPrint that would make a PDF, after loading it and Pango as `--pdf` does.
    Raises PdfUnavailable, saying why there is none."""
    return str(_weasyprint().__version__)


def _refusing_fetcher(weasyprint: Any) -> Any:  # Any: WeasyPrint has no type information
    """A URL fetcher that refuses every URL, and stops the render rather than leave a gap: the
    template refers to no URL, so one asked for is ledger text that escaped it."""
    fatal: Any = weasyprint.urls.FatalURLFetchingError

    def fetch(self: Any, url: str, headers: Any = None) -> Any:
        raise fatal(f"a run's PDF loads nothing; refused {url[:200]!r}")

    return type("RefusingFetcher", (weasyprint.URLFetcher,), {"fetch": fetch})()


def _breakable(text: str) -> str:
    """`text` with a zero-width space every BREAK_EVERY characters of a run with no space."""
    return _LONG_RUN.sub(
        lambda m: "\u200b".join(
            m[0][i : i + BREAK_EVERY] for i in range(0, len(m[0]), BREAK_EVERY)
        ),
        text,
    )


def _short(text: str) -> str:
    """A value for a fact or a table cell: its first VALUE_LIMIT characters, breakable."""
    if len(text) > VALUE_LIMIT:
        left = len(text) - VALUE_LIMIT
        text = f"{text[:VALUE_LIMIT]}… ({left:,} more characters; the ledger holds them)"
    return _breakable(text)


def _short_keys[V](values: dict[str, V]) -> dict[str, V]:
    """`values` with each name made short as `_short` does; two that end up the same are kept
    apart by a number, so no row takes another's place."""
    shown: dict[str, V] = {}
    for name, value in values.items():
        key, n = _short(name), 1
        while key in shown:
            n += 1
            key = f"{_short(name)} ({n})"
        shown[key] = value
    return shown


def _for_pdf(page: Page) -> Page:
    """`page` with every value from the ledger that it shows short enough, and breakable, to
    lay out."""
    return dataclasses.replace(
        page,
        title=_short(page.title),
        run_id=_short(page.run_id),
        name=_short(page.name),
        principal=_short(page.principal),
        client=_short(page.client),
        agent=_short(page.agent),
        outcome_reason=_short(page.outcome_reason),
        verify_command=_breakable(page.verify_command),
        ledger_file=_short(page.ledger_file),
        models=_short_keys(page.models),
        tools=_short_keys(page.tools),
        refusals=[
            dataclasses.replace(r, rule=_short(r.rule), reason=_short(r.reason))
            for r in page.refusals
        ],
        failures=[dataclasses.replace(f, error=_short(f.error)) for f in page.failures],
        rows=[
            dataclasses.replace(
                row,
                actor=_short(row.actor),
                what=_breakable(row.what),
                id=_short(row.id),
                parent=_short(row.parent),
                detail=_breakable(row.detail),
                detail_end=_breakable(row.detail_end),
            )
            for row in page.rows
        ],
    )


def render_pdf(ledger: Path, pubkey: Path | None = None, now: datetime | None = None) -> bytes:
    """The PDF for `ledger`. Raises as `html.build` does, PageError for a document that asked
    for a URL, and PdfUnavailable."""
    weasyprint = _weasyprint()
    page = build(ledger, pubkey, now, limit=PDF_DETAIL_LIMIT, head=PDF_DETAIL_HEAD, every=False)
    html = template(TEMPLATE).render(page=_for_pdf(page), detail_limit=PDF_DETAIL_LIMIT)
    document = weasyprint.HTML(string=html, url_fetcher=_refusing_fetcher(weasyprint))
    try:
        pdf: bytes = document.write_pdf()
    except weasyprint.urls.FatalURLFetchingError as exc:  # a BaseException: stop it here
        raise PageError(f"{ledger}: {exc}") from exc
    return pdf


def write_pdf(
    ledger: Path, pubkey: Path | None = None, out: Path | None = None, now: datetime | None = None
) -> Path:
    """Write the PDF for `ledger` to `out` (default: beside the ledger), mode 0600, replacing
    any there whole, as the page is written. Returns where it went. Raises as `render_pdf`
    does, and OSError."""
    pdf = render_pdf(ledger, pubkey, now)
    out = out or pdf_path(ledger)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)  # 0600
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(pdf)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out
