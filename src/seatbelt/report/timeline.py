"""Reconstruction: turn a ledger back into something a reviewer can read."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from seatbelt.gateway.formats import as_dict, as_dicts
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import read_events
from seatbelt.terminal import printable


def summary(event: Event) -> str:
    """One line for `event`, as `reconstruct` prints it and the HTML page shows it."""
    # escaped before it is cut: a cut escape sequence would leave the terminal inside it
    return " ".join(printable(_describe(event)).split())[:80]


def _failed(error: object) -> str:
    return f"FAILED: {error}" if error else "FAILED"


_PROVENANCE = {  # compliance.provenance types, as a reader should take them
    "client_asserted": "unverified",
    "synthetic_marker": "marker",
    "content_unavailable": "unavailable",
}


def _provenance(attrs: dict[str, Any]) -> str:
    """A leading mark for an imported message that is not verified model or user content."""
    p = as_dict(attrs.get("compliance.provenance"))
    if not p:
        return ""
    label = _PROVENANCE.get(str(p.get("type")), str(p.get("type")))
    return f"[{label}: {p['reason']}] " if p.get("reason") else f"[{label}] "


def _text(response: object) -> str:
    """The text blocks of an imported answer, which records `{role, content: [blocks]}`."""
    blocks = as_dicts(as_dict(response).get("content"))
    return " ".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")


def _describe(event: Event) -> str:
    a = event.attrs
    match event.kind:
        case Kind.RUN_START:
            return f"seatbelt {a.get('harness.version')}"
        case Kind.RUN_END:
            return "ok" if a.get("run.ok") else _failed(a.get("run.error"))
        case Kind.USER_MESSAGE:
            msgs = a.get("gen_ai.input.messages", [])
            return _provenance(a) + (str(msgs[-1]["content"]) if msgs else "")
        case Kind.MODEL_REQUEST:
            return f"-> {a.get('gen_ai.request.model')}"
        case Kind.MODEL_RESPONSE:
            if a.get("error"):
                return str(a["error"])
            if "compliance.message_id" in a:  # imported: the answer's text, not token counts
                model = a.get("gen_ai.response.model") or "unknown"
                return f"<- {model}: {_provenance(a)}{_text(a.get('gen_ai.response'))}"
            out = a.get("gen_ai.usage.output_tokens", "?")
            return f"<- {a.get('gen_ai.response.model')} ({out} out)"
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
    table = Table(title=Text(f"run {printable(path.stem)}"), show_lines=False)
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
            summary(e),
            e.hash[:10],
        )
        # ledger text is data: never Rich markup, nor terminal control sequences
        table.add_row(*(Text(printable(c)) for c in cells))
    console.print(table)
