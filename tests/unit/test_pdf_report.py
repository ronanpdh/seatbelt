"""Each run's PDF: what it shows, that ledger text stays text and nothing is loaded, that only a
ledger that verifies gets one, how it is written, and what the CLI says without the extra."""

import importlib
import os
import stat
import time
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pypdf
import pytest
from tests.unit.test_html_report import HOSTILE, _keys, _run  # pyright: ignore[reportPrivateUsage]
from typer.testing import CliRunner

from seatbelt.cli import app
from seatbelt.report import html, pdf, pdf_path
from seatbelt.report.html import PageError, build
from seatbelt.report.pack import build as build_pack
from seatbelt.report.pdf import PDF_DETAIL_LIMIT, PdfUnavailable, render_pdf, write_pdf


@pytest.fixture(autouse=True)
def _weasyprint() -> None:
    """The PDF tests need Pango, which the dev group cannot install. CI installs it."""
    try:
        importlib.import_module("weasyprint")
    except OSError as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"WeasyPrint cannot load Pango here: {exc}")


def _text(path: Path) -> str:
    return "\n".join(page.extract_text() for page in pypdf.PdfReader(path).pages)


def test_the_pdf_shows_the_run_what_was_checked_and_each_event(tmp_path: Path) -> None:
    ledger = _run(tmp_path)
    _, pub = _keys(tmp_path)
    text = _text(write_pdf(ledger, pub))
    for shown in (
        "claude-1a2b",
        "alice-claude-1a2b-0001",
        "When this PDF was made",
        "attested",
        "This PDF is a view, not evidence",
        "seatbelt verify",
        "claude-opus-5-5-20260901",
        "lookup_order",
        "Refused and failed events, in full",
    ):
        assert shown in text, shown
    page = build(ledger, pub)
    for row in page.rows:  # one line each, in order
        assert f"{row.seq} " in text and row.kind in text
    full = text.split("Refused and failed events, in full", 1)[1]
    assert "Bash is denied: Refund order 1001" in full  # the refusal, in full
    assert "overloaded: Refund order 1001" in full  # the failed call, in full
    assert '"status"' not in text  # the tool's result: one line only
    metadata = pypdf.PdfReader(pdf_path(ledger)).metadata
    assert metadata is not None and metadata.title == "claude-1a2b · seatbelt run"


def test_only_refused_and_failed_events_carry_their_detail(tmp_path: Path) -> None:
    page = build(_run(tmp_path), every=False, limit=PDF_DETAIL_LIMIT)
    detailed = {row.kind for row in page.rows if row.detail}
    assert detailed == {"policy.check", "model.response"}
    assert all(row.detail for row in page.rows if row.denied or row.failed)


def test_a_long_refused_or_failed_event_shows_its_start_and_end(tmp_path: Path) -> None:
    ledger = _run(tmp_path, text="x" * (3 * PDF_DETAIL_LIMIT))
    page = build(ledger, every=False, limit=PDF_DETAIL_LIMIT, head=pdf.PDF_DETAIL_HEAD)
    cut = [row for row in page.rows if row.cut]
    assert cut and all(row.denied or row.failed for row in cut)
    assert "bytes from the middle of this event are left out" in _text(write_pdf(ledger))


def test_a_long_unbroken_value_is_cut_and_breakable_and_quick_to_lay_out(tmp_path: Path) -> None:
    """WeasyPrint's layout of one unbroken run of text takes time that grows faster than its
    length, and took minutes in a table cell: the PDF never gives it one."""
    ledger = _run(tmp_path, text="x" * 200_000)
    page = pdf._for_pdf(build(ledger, every=False, limit=PDF_DETAIL_LIMIT))  # pyright: ignore[reportPrivateUsage]
    shown = [page.title, page.client, *(f.error for f in page.failures)]
    shown += [r.reason for r in page.refusals] + [row.detail for row in page.rows]
    for text in shown:
        assert all(len(run) <= pdf.BREAK_EVERY for run in text.replace("\u200b", " ").split())
    assert all(len(f.error) < pdf.VALUE_LIMIT + 100 for f in page.failures)
    assert "more characters; the ledger holds them" in page.client
    started = time.monotonic()
    write_pdf(ledger)
    assert time.monotonic() - started < 60  # under a second here; minutes before


def test_a_long_run_id_and_names_that_cut_alike_are_shown_apart(tmp_path: Path) -> None:
    page = build(_run(tmp_path), every=False, limit=PDF_DETAIL_LIMIT)
    long = "m" * 2 * pdf.VALUE_LIMIT
    page.run_id = "r" * 200_000
    page.models = {f"{long}a": page.total, f"{long}b": page.total}
    shown = pdf._for_pdf(page)  # pyright: ignore[reportPrivateUsage]
    assert len(shown.run_id) < pdf.VALUE_LIMIT + 200
    assert len(shown.models) == 2


@pytest.mark.parametrize(
    ("env", "dirs", "expected"),
    [
        # unset: macOS's own fallback first, then the folders that exist
        ({}, {"/opt/homebrew/lib"}, "~/lib:/usr/local/lib:/lib:/usr/lib:/opt/homebrew/lib"),
        ({}, {"/opt/local/lib"}, "~/lib:/usr/local/lib:/lib:/usr/lib:/opt/local/lib"),
        # set: kept, and added to
        ({"DYLD_FALLBACK_LIBRARY_PATH": "/x"}, {"/opt/homebrew/lib"}, "/x:/opt/homebrew/lib"),
        (
            {"HOMEBREW_PREFIX": "/brew"},
            {"/brew/lib"},
            "~/lib:/usr/local/lib:/lib:/usr/lib:/brew/lib",
        ),
        # nothing to add
        ({"DYLD_FALLBACK_LIBRARY_PATH": "/opt/homebrew/lib"}, {"/opt/homebrew/lib"}, None),
        ({}, set[str](), None),
    ],
)
def test_on_macos_the_folders_homebrew_and_macports_use_are_searched(
    env: dict[str, str], dirs: set[str], expected: str | None
) -> None:
    found = pdf.mac_library_path(env, dirs.__contains__)
    home = os.path.expanduser("~")
    assert found == (expected.replace("~", home) if expected else None)


def test_ledger_text_stays_text_and_nothing_is_loaded(tmp_path: Path) -> None:
    """Any URL the document asked for would stop the render, so a PDF made at all loaded
    nothing; and the text is there as text."""
    ledger = _run(tmp_path, text=HOSTILE + "‮\x1b[2J")
    text = _text(write_pdf(ledger))
    assert '<script>document.title="PWNED"</script>' in text
    assert "<img src=x onerror=alert(1)>" in text
    assert "\\u202e" in text and "‮" not in text


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="http://127.0.0.1:9/x.png">',
        '<img src="file:///etc/passwd">',
        '<img src="data:image/png;base64,iVBORw0KGgo=">',
        '<style>@import url("http://127.0.0.1:9/a.css");</style><p>x</p>',
        '<p style="background:url(http://127.0.0.1:9/b.png)">x</p>',
        '<link rel="stylesheet" href="http://127.0.0.1:9/c.css"><p>x</p>',
        "<style>@font-face{font-family:x;src:url(http://127.0.0.1:9/f.woff)}</style><p>x</p>",
    ],
)
def test_a_document_that_asks_for_any_url_is_not_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, markup: str
) -> None:
    """As if a value had escaped the template: the fetcher refuses the URL and stops the
    render, so there is no PDF."""

    class Escaped:
        def render(self, **_: Any) -> str:
            return f"<!doctype html><html><body>{markup}</body></html>"

    def escaped(name: str) -> Escaped:
        return Escaped()

    monkeypatch.setattr(pdf, "template", escaped)
    ledger = _run(tmp_path)
    with pytest.raises(PageError, match="loads nothing"):
        write_pdf(ledger)
    assert not pdf_path(ledger).exists()


def test_a_broken_or_tampered_ledger_gets_no_pdf(tmp_path: Path) -> None:
    ledger = _run(tmp_path)
    ledger.write_text(ledger.read_text().replace("Refund order 1001", "Refund order 9999", 1))
    with pytest.raises(PageError, match="chain broken"):
        write_pdf(ledger)
    assert not pdf_path(ledger).exists()


def test_the_pdf_is_written_whole_owner_only_and_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _run(tmp_path)
    out = write_pdf(ledger)
    assert out == pdf_path(ledger) == ledger.with_suffix(".pdf")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    out.write_bytes(b"stale")
    os.chmod(out, 0o644)
    assert write_pdf(ledger) == out and out.read_bytes().startswith(b"%PDF-")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def crash(src: str, dst: str) -> None:
        raise OSError("disk full")

    out.unlink()
    monkeypatch.setattr(pdf.os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        write_pdf(ledger)
    assert [p.name for p in ledger.parent.iterdir() if p.suffix in (".tmp", ".pdf")] == []


def test_reconstruct_pdf_writes_one_and_refuses_a_tampered_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))  # not this machine's runs
    monkeypatch.chdir(tmp_path)
    ledger = _run(tmp_path)
    runner = CliRunner()
    out = runner.invoke(app, ["reconstruct", str(ledger), "--pdf"])
    assert out.exit_code == 0, out.output
    assert pdf_path(ledger).exists()  # beside the ledger, where erase finds it
    out = runner.invoke(app, ["reconstruct", str(ledger), "--pdf", "--out", "r.pdf"])
    assert out.exit_code == 0 and (tmp_path / "r.pdf").read_bytes().startswith(b"%PDF-")
    out = runner.invoke(app, ["reconstruct", str(ledger), "--pdf", "--out", "r.html"])
    assert out.exit_code == 1 and "ends in .pdf" in out.output
    out = runner.invoke(app, ["reconstruct", str(ledger), "--pdf", "--html"])
    assert out.exit_code == 1 and "give one" in out.output
    out = runner.invoke(app, ["reconstruct", str(ledger), "--pdf", "--json"])
    assert out.exit_code == 1
    ledger.write_text(ledger.read_text().replace("Refund", "Rebate", 1))
    out = runner.invoke(app, ["reconstruct", str(ledger), "--pdf", "--out", "t.pdf"])
    assert out.exit_code == 1 and not (tmp_path / "t.pdf").exists()


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (ImportError("No module named 'weasyprint'"), "seatbelt-ai[pdf]"),
        (OSError("cannot load library 'libpango-1.0-0'"), "cannot load a library it needs"),
    ],
)
def test_without_the_extra_the_cli_says_how_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, said: str
) -> None:
    real = importlib.import_module

    def missing(name: str, package: str | None = None) -> ModuleType:
        if name == "weasyprint":
            raise error
        return real(name, package)

    monkeypatch.setattr(pdf.importlib, "import_module", missing)
    ledger = _run(tmp_path)
    with pytest.raises(PdfUnavailable):
        render_pdf(ledger)
    out = CliRunner().invoke(app, ["reconstruct", str(ledger), "--pdf"])
    assert out.exit_code == 1 and said in " ".join(out.output.split())
    assert not pdf_path(ledger).exists()


def test_pack_leaves_pdfs_out(tmp_path: Path) -> None:
    ledger = _run(tmp_path)
    write_pdf(ledger)
    build_pack(ledger.parent, tmp_path / "pack.zip")
    with zipfile.ZipFile(tmp_path / "pack.zip") as zf:
        assert not [n for n in zf.namelist() if n.endswith(".pdf")]


def test_the_html_page_shares_its_parts_with_the_pdf(tmp_path: Path) -> None:
    """The page's header, checks and summary come from the parts the PDF uses."""
    page = html.render(_run(tmp_path))
    assert "When this page was made" in page and "This page is a view, not evidence" in page
