"""Reports over ledgers: the terminal timeline, the HTML page, usage, the evidence pack."""

from pathlib import Path


def page_path(ledger: Path) -> Path:
    """Where a run's HTML page goes: beside its ledger, as its signature does."""
    return ledger.with_suffix(".html")
