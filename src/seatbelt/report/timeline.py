"""Reconstruction: turn a ledger back into something a reviewer can read."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import read_events


def _summary(event: Event) -> str:
    return " ".join(_describe(event).split())[:80]


def _failed(error: object) -> str:
    return f"FAILED: {error}" if error else "FAILED"


def _describe(event: Event) -> str:
    a = event.attrs
    match event.kind:
        case Kind.RUN_START:
            return f"seatbelt {a.get('harness.version')}"
        case Kind.RUN_END:
            return "ok" if a.get("run.ok") else _failed(a.get("run.error"))
        case Kind.USER_MESSAGE:
            msgs = a.get("gen_ai.input.messages", [])
            return str(msgs[-1]["content"]) if msgs else ""
        case Kind.MODEL_REQUEST:
            return f"-> {a.get('gen_ai.request.model')}"
        case Kind.MODEL_RESPONSE:
            out = a.get("gen_ai.usage.output_tokens", "?")
            return a.get("error") or f"<- {a.get('gen_ai.response.model')} ({out} out)"
        case Kind.TOOL_CALL:
            return f"{a.get('gen_ai.tool.name')}({a.get('gen_ai.tool.call.arguments')})"
        case Kind.TOOL_RESULT:
            return a.get("error") or str(a.get("gen_ai.tool.call.result"))
        case Kind.POLICY_CHECK:
            return f"{'ALLOW' if a.get('policy.allowed') else 'DENY'}: {a.get('policy.reason')}"
        case Kind.DECISION:
            return f"[authority: {a.get('decision.authority')}] {a.get('decision.summary')}"
        case Kind.ACTION:
            return f"{a.get('action.description')} -> {a.get('action.target')}"
        case Kind.OUTCOME:
            return f"{'ok' if a.get('outcome.success') else 'FAILED'}: {a.get('outcome.summary')}"
        case _:
            return ""


def timeline(path: Path, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title=Text(f"run {path.stem}"), show_lines=False)
    table.add_column("seq", justify="right", style="dim")
    table.add_column("time", style="dim")
    table.add_column("kind")
    table.add_column("actor")
    table.add_column("what")
    table.add_column("hash", style="dim")
    for e in read_events(path):
        actor = f"{e.actor.type}:{e.actor.id}" + (f"@{e.actor.version}" if e.actor.version else "")
        cells = (
            str(e.seq),
            e.ts.strftime("%H:%M:%S.%f")[:-3],
            e.kind,
            actor,
            _summary(e),
            e.hash[:10],
        )
        table.add_row(*(Text(c) for c in cells))  # ledger text is data, never Rich markup
    console.print(table)
