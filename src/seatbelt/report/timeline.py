"""Reconstruction: turn a ledger back into something a reviewer can read."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import Ledger


def _summary(event: Event) -> str:
    a = event.attrs
    match event.kind:
        case Kind.USER_MESSAGE:
            msgs = a.get("gen_ai.input.messages", [])
            return str(msgs[-1]["content"])[:80] if msgs else ""
        case Kind.MODEL_REQUEST:
            return f"-> {a.get('gen_ai.request.model')}"
        case Kind.MODEL_RESPONSE:
            out = a.get("gen_ai.usage.output_tokens", "?")
            return f"<- {a.get('gen_ai.response.model')} ({out} out)"
        case Kind.TOOL_CALL:
            return f"{a.get('gen_ai.tool.name')}({a.get('gen_ai.tool.call.arguments')})"[:80]
        case Kind.TOOL_RESULT:
            return (a.get("error") or str(a.get("gen_ai.tool.call.result")))[:80]
        case Kind.POLICY_CHECK:
            return f"{'ALLOW' if a.get('policy.allowed') else 'DENY'}: {a.get('policy.reason')}"
        case Kind.DECISION:
            return f"{a.get('decision.summary')} [authority: {a.get('decision.authority')}]"
        case Kind.ACTION:
            return f"{a.get('action.description')} -> {a.get('action.target')}"
        case Kind.OUTCOME:
            return f"{'ok' if a.get('outcome.success') else 'FAILED'}: {a.get('outcome.summary')}"
        case _:
            return ""


def timeline(path: Path, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title=f"run {path.stem}", show_lines=False)
    table.add_column("seq", justify="right", style="dim")
    table.add_column("time", style="dim")
    table.add_column("kind")
    table.add_column("actor")
    table.add_column("what")
    table.add_column("hash", style="dim")
    for e in Ledger(path, run_id=path.stem).read():
        actor = f"{e.actor.type}:{e.actor.id}" + (f"@{e.actor.version}" if e.actor.version else "")
        table.add_row(
            str(e.seq), e.ts.strftime("%H:%M:%S.%f")[:-3], e.kind, actor, _summary(e), e.hash[:10]
        )
    console.print(table)
