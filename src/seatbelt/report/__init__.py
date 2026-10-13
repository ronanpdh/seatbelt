"""Reports over ledgers: the terminal timeline, the HTML page and PDF, usage, the evidence pack."""

from pathlib import Path


def page_path(ledger: Path) -> Path:
    """Where a run's HTML page goes: beside its ledger, as its signature does."""
    return ledger.with_suffix(".html")


def pdf_path(ledger: Path) -> Path:
    """Where a run's PDF goes when made: beside its ledger, as its page does."""
    return ledger.with_suffix(".pdf")
