from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from seatbelt import __version__
from seatbelt.record.recorder import Recorder
from seatbelt.report.timeline import timeline
from seatbelt.verify.chain import verify_file

app = typer.Typer(help="Attributable, reconstructable, provable records of agent interactions.")
console = Console()


@app.callback()
def main() -> None:
    """Agent audit harness."""


@app.command()
def version() -> None:
    """Print the harness version."""
    console.print(__version__)


@app.command()
def verify(ledger: Path) -> None:
    """Check a run ledger's hash chain. Exit code 1 if it has been altered."""
    verdict = verify_file(ledger)
    if verdict.ok:
        console.print(f"[green]ok[/] {verdict.events} events, chain intact")
        return
    where = "" if verdict.first_bad_seq is None else f" at seq {verdict.first_bad_seq}"
    console.print(f"[red]BROKEN[/]{where}: {escape(verdict.reason or '')}")
    raise typer.Exit(code=1)


@app.command()
def reconstruct(ledger: Path) -> None:
    """Print the run as a timeline a reviewer can read."""
    timeline(ledger, console)


@app.command()
def demo(out: Path = Path("runs")) -> None:
    """Record a scripted example run so you can try verify and reconstruct."""
    with Recorder.start(out, agent_id="demo-agent", agent_version="0.1") as rec:
        u = rec.user_message("user-42", "Refund order 1001, key sk-ant-abcdefghijklmnopqrstuvwxyz")
        with rec.model_call("claude-sonnet-4-5", {"messages": ["..."]}, provider="anthropic") as m:
            m.respond({"tool_use": "lookup_order"}, usage={"input_tokens": 40, "output_tokens": 12})
        with rec.tool_call("lookup_order", {"order": 1001}) as t:
            t.result({"total": 49.0, "status": "delivered"})
        p = rec.policy_check("refund-limit", u.id, allowed=True, reason="49.00 under 100.00 limit")
        d = rec.decision("issue full refund", authority="agent:auto-under-100", basis=[u.id, p.id])
        rec.action("POST /refunds", target="payments-api", decision_id=d.id)
        rec.outcome("refund issued", success=True)
        console.print(f"wrote {rec.ledger.path}")
