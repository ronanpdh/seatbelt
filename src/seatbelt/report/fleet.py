"""Aggregate a runs directory: who used what, how much, and what was refused.

Only ledgers whose chain verifies are counted: a broken one's numbers cannot be trusted, so
it is listed, not summed."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import LedgerError, read_events
from seatbelt.verify.attest import Attestation, verify_attestation
from seatbelt.verify.chain import verify_events


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runs: int = 0
    calls: int = 0  # model responses, errors included
    input_tokens: int = 0
    output_tokens: int = 0
    denials: int = 0  # policy checks that refused


class Fleet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runs: int = 0
    by_principal: dict[str, Usage] = Field(default_factory=dict[str, Usage])
    by_model: dict[str, Usage] = Field(default_factory=dict[str, Usage])
    by_tool: dict[str, int] = Field(default_factory=dict[str, int])
    failed: list[str] = Field(default_factory=list[str])  # ended with run.ok false
    incomplete: list[str] = Field(default_factory=list[str])  # no run.end: open or truncated
    unattested: list[str] = Field(default_factory=list[str])  # no signed sidecar
    forged: list[str] = Field(default_factory=list[str])  # sidecar fails the given key
    broken: list[str] = Field(default_factory=list[str])  # unreadable or chain fails; not counted


def _tokens(event: Event, name: str) -> int:
    value = event.attrs.get(f"gen_ai.usage.{name}")
    return value if isinstance(value, int) else 0


def fleet(runs: Path, pubkey: Path | None = None) -> Fleet:
    """`pubkey` checks every attestation; without it only their presence is checked."""
    out = Fleet()
    tools: dict[str, int] = defaultdict(int)
    for path in sorted(runs.glob("*.jsonl")):
        try:
            events = list(read_events(path))
        except (OSError, UnicodeDecodeError, LedgerError):
            out.broken.append(path.stem)
            continue
        if not events:
            continue
        if not verify_events(events).ok:
            out.broken.append(events[0].run_id)
            continue
        run_id = events[0].run_id
        out.runs += 1
        principal = str(events[0].attrs.get("principal.id") or "unknown")
        who = out.by_principal.setdefault(principal, Usage())
        who.runs += 1
        requested: dict[str, str] = {}  # model.request id -> model asked for
        models_used: set[str] = set()
        for e in events:
            if e.kind is Kind.MODEL_REQUEST:
                requested[e.id] = str(e.attrs.get("gen_ai.request.model"))
            elif e.kind is Kind.MODEL_RESPONSE:
                model = str(e.attrs.get("gen_ai.response.model"))
                m = out.by_model.setdefault(model, Usage())
                models_used.add(model)
                for u in (who, m):
                    u.calls += 1
                    u.input_tokens += _tokens(e, "input_tokens")
                    u.output_tokens += _tokens(e, "output_tokens")
            elif e.kind is Kind.TOOL_CALL:
                tools[str(e.attrs.get("gen_ai.tool.name"))] += 1
            elif e.kind is Kind.POLICY_CHECK and e.attrs.get("policy.allowed") is False:
                who.denials += 1
                if e.parent_id in requested:  # a refused request counts against its model
                    model = requested[e.parent_id]
                    out.by_model.setdefault(model, Usage()).denials += 1
                    models_used.add(model)
        for model in models_used:
            out.by_model[model].runs += 1
        last = events[-1]
        if last.kind is not Kind.RUN_END:
            out.incomplete.append(run_id)
        elif last.attrs.get("run.ok") is False:
            out.failed.append(run_id)
        status = verify_attestation(path, pubkey).status
        if status is Attestation.UNATTESTED:
            out.unattested.append(run_id)
        elif status is Attestation.FORGED:
            out.forged.append(run_id)
    out.by_tool = dict(sorted(tools.items()))
    return out
