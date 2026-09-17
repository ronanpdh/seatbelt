import importlib
import os
import sys
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Column, Table

from seatbelt import __version__
from seatbelt.attest.manifest import AttestError, sidecar
from seatbelt.attest.sign import Signer
from seatbelt.attest.sign import attest as sign_ledger
from seatbelt.attest.sign import keygen as make_keys
from seatbelt.record.recorder import Recorder
from seatbelt.report.timeline import timeline
from seatbelt.scenarios.model import OWASP_AGENTIC, ScenarioError, load_corpus
from seatbelt.scenarios.runner import Target
from seatbelt.scenarios.runner import run as run_corpus
from seatbelt.verify.attest import Attestation, AttestVerdict, verify_attestation
from seatbelt.verify.chain import Verdict, verify_file

app = typer.Typer(help="Attributable, reconstructable, provable records of agent interactions.")
console = Console()


@app.callback()
def main() -> None:
    """Agent audit harness."""


@app.command()
def version() -> None:
    """Print the harness version."""
    console.print(__version__)


PubKey = Annotated[Path | None, typer.Option(help="public key from keygen; checks the attestation")]


def _check(ledger: Path, pubkey: Path | None = None) -> tuple[Verdict, AttestVerdict]:
    """Exit 1 on a broken chain, a forged attestation, or none when a key was given."""
    verdict = verify_file(ledger)
    if not verdict.ok:
        where = "" if verdict.first_bad_seq is None else f" at seq {verdict.first_bad_seq}"
        console.print(f"[red]BROKEN[/]{where}: {escape(verdict.reason or '')}")
        raise typer.Exit(code=1)
    try:
        att = verify_attestation(ledger, pubkey)
    except AttestError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if att.status is Attestation.FORGED:
        console.print(f"[red]FORGED[/]: {escape(att.reason or '')}")
        raise typer.Exit(code=1)
    if att.status is Attestation.UNATTESTED and pubkey is not None:
        console.print(f"[red]UNATTESTED[/]: no {escape(str(sidecar(ledger)))}")
        raise typer.Exit(code=1)
    if att.status is Attestation.UNCHECKED:
        console.print(f"[yellow]UNCHECKED[/] {escape(att.reason or '')}")
    if not verdict.complete:
        console.print(
            f"[yellow]INCOMPLETE[/] {verdict.events} events, chain intact but no matching "
            "run.end: truncated or still running"
        )
    return verdict, att


@app.command()
def verify(ledger: Path, pubkey: PubKey = None) -> None:
    """Check a run ledger's hash chain and attestation. Exit 1 if altered, forged or incomplete."""
    verdict, att = _check(ledger, pubkey)
    if not verdict.complete:
        raise typer.Exit(code=1)
    console.print(f"[green]ok[/] {verdict.events} events, chain intact, {att.status}")


@app.command()
def reconstruct(ledger: Path, pubkey: PubKey = None) -> None:
    """Print the run as a timeline a reviewer can read. Refuses an altered ledger."""
    _check(ledger, pubkey)
    timeline(ledger, console)


@app.command()
def keygen(directory: Annotated[Path, typer.Argument()] = Path(".")) -> None:
    """Write an Ed25519 signing key (seatbelt.key, mode 0600) and its public key (seatbelt.pub)."""
    try:
        key, pub = make_keys(directory)
    except AttestError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"wrote {key} and {pub}")


@app.command()
def attest(
    ledger: Path, key: Annotated[Path, typer.Option(help="private key from keygen")]
) -> None:
    """Sign a finished ledger into <run id>.attest.json. Refuses a broken or incomplete chain."""
    if sidecar(ledger).exists():
        console.print(f"[red]{escape(str(sidecar(ledger)))} exists; delete it to re-sign[/]")
        raise typer.Exit(code=1)
    verdict, _ = _check(ledger)
    if not verdict.complete:
        raise typer.Exit(code=1)
    try:
        out = sign_ledger(ledger, Signer.from_file(key))
    except AttestError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"wrote {out}")


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


def _import_target(spec: str) -> Target:
    sys.path.insert(0, os.getcwd())  # console scripts do not put the cwd on the path
    module, _, attr = spec.partition(":")
    try:  # user code runs at import, so anything can go wrong
        fn = getattr(importlib.import_module(module), attr)
    except Exception as exc:
        console.print(f"[red]cannot import target {escape(spec)}[/]: {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    if not callable(fn):
        console.print(f"[red]target {escape(spec)} is not callable[/]")
        raise typer.Exit(code=2)
    return cast(Target, fn)  # the callable's signature cannot be checked at runtime


@app.command()
def scenarios(
    corpus: Path,
    target: Annotated[
        str | None, typer.Option(help="module:function that drives your agent")
    ] = None,
    out: Path = Path("runs"),
    key: Annotated[
        Path | None, typer.Option(help="private key from keygen; signs each ledger")
    ] = None,
    list_: Annotated[bool, typer.Option("--list", help="show the corpus and exit")] = False,
) -> None:
    """Run the adversarial corpus against a target. Exit 1 on any finding."""
    try:
        pack = load_corpus(corpus)
    except ScenarioError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if list_:
        table = Table(Column("scenario", no_wrap=True), "owasp", "severity", "title")
        for s in pack:
            owasp = ", ".join(f"{i} {OWASP_AGENTIC[i]}" for i in s.owasp) or "control"
            table.add_row(s.id, owasp, s.severity, escape(s.title))
        console.print(table)
        return
    if target is None:
        console.print("[red]pass --target module:function, or --list to see the corpus[/]")
        raise typer.Exit(code=2)
    fn = _import_target(target)
    try:
        signer = Signer.from_file(key) if key else None
        report = run_corpus(corpus, fn, out, signer=signer)
    except (AttestError, ScenarioError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    table = Table(Column("scenario", no_wrap=True), "owasp", "severity", "result", "findings")
    for s, r in zip(pack, report.results, strict=True):
        verdict = "[green]PASS[/]" if r.findings == 0 else "[red]FAIL[/]"
        table.add_row(s.id, ", ".join(s.owasp) or "control", s.severity, verdict, str(r.findings))
    console.print(table)
    for f in report.findings:
        evidence = escape(", ".join(f.evidence))
        console.print(f"  {escape(f.scenario_id)}: {escape(f.check)} (evidence: {evidence})")
    console.print(f"wrote {escape(str(out / 'findings.json'))}")
    if report.findings:
        raise typer.Exit(code=1)
