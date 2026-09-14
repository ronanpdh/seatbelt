import typer
from rich.console import Console

from seatbelt import __version__

app = typer.Typer(help="Adversarial pre-deployment testing for AI agents.")
console = Console()


@app.callback()
def main() -> None:
    """Agent assurance harness."""


@app.command()
def version() -> None:
    """Print the harness version."""
    console.print(__version__)


@app.command()
def run(scenario: str) -> None:
    """Run a scenario against a target (not implemented yet)."""
    console.print(f"[yellow]would run[/] {scenario}")
