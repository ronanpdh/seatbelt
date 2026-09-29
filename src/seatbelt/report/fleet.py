"""Aggregate a runs directory: who used what, how much, and what was refused.

Only ledgers whose chain verifies are counted: a broken one's numbers cannot be trusted, so
it is listed, not summed. Nor are forged ones, or, when a key is given, ledgers that ended but
carry no signature. A signature whose ledger is gone is listed as missing; a ledger deleted
together with its signature leaves nothing behind, so no report can tell it was there."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field

from seatbelt.attest.manifest import sidecar
from seatbelt.erase import erased_hashes, erased_sidecar
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


class People:
    """Who each principal id belongs to. One person can have several: an issued gateway key,
    an identity provider's subject, an Anthropic user id from the Compliance API importer.
    Only ids listed explicitly are joined; matching on e-mail is never done, since an e-mail
    address is not an immutable identity and some providers do not verify it."""

    def __init__(self, person_of: Mapping[str, str] | None = None) -> None:
        self._person_of = dict(person_of or {})

    @classmethod
    def load(cls, path: Path) -> People:
        """`people:` in a YAML file: each person's name, then the principal ids that are
        theirs. Raises ValueError, naming the file, for anything else."""
        try:
            data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValueError(f"{path}: {exc}") from exc
        people = cast(dict[str, Any], data).get("people") if isinstance(data, dict) else None
        if not isinstance(people, dict):
            raise ValueError(f"{path}: expected `people:`, each person's name then their ids")
        person_of: dict[str, str] = {}
        for name, ids in cast(dict[Any, Any], people).items():
            name = str(name)
            if not isinstance(ids, list):
                raise ValueError(f"{path}: {name}: expected a list of principal ids")
            for principal in cast(list[Any], ids):
                if not isinstance(principal, str) or not principal:
                    raise ValueError(f"{path}: {name}: expected a list of principal ids")
                other = person_of.setdefault(principal, name)
                if other != name:
                    raise ValueError(f"{path}: {principal} is listed for both {other} and {name}")
        return cls(person_of)

    def person(self, principal: str) -> str:
        return self._person_of.get(principal, principal)

    def ids_of(self, person: str) -> list[str]:
        """The principal ids listed for `person`; none if the file does not name them."""
        return sorted(i for i, p in self._person_of.items() if p == person)


class Fleet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runs: int = 0
    by_principal: dict[str, Usage] = Field(default_factory=dict[str, Usage])  # by person
    # each person given in a people file, with the principal ids of theirs that were seen
    people: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    by_model: dict[str, Usage] = Field(default_factory=dict[str, Usage])
    by_tool: dict[str, int] = Field(default_factory=dict[str, int])
    failed: list[str] = Field(default_factory=list[str])  # ended with run.ok false
    incomplete: list[str] = Field(default_factory=list[str])  # no run.end: open or truncated
    unattested: list[str] = Field(default_factory=list[str])  # no signed sidecar
    forged: list[str] = Field(default_factory=list[str])  # fails the given key; not counted
    # with a key given: ended but no sidecar, as when one is edited and its signature deleted;
    # not counted
    unsigned: list[str] = Field(default_factory=list[str])
    broken: list[str] = Field(default_factory=list[str])  # unreadable or chain fails; not counted
    missing: list[str] = Field(default_factory=list[str])  # a sidecar whose ledger is gone


def _tokens(event: Event, name: str) -> int:
    value = event.attrs.get(f"gen_ai.usage.{name}")
    return value if isinstance(value, int) else 0


def fleet(
    runs: Path | Iterable[Path], pubkey: Path | None = None, people: People | None = None
) -> Fleet:
    """`runs` is one runs directory or several, e.g. the gateway's and the Compliance API
    importer's. `pubkey` checks every attestation; without it only their presence is checked.
    `people` joins principal ids that belong to one person."""
    out = Fleet()
    people = people or People()
    seen: dict[str, set[str]] = defaultdict(set)
    tools: dict[str, int] = defaultdict(int)
    dirs = [runs] if isinstance(runs, Path) else list(runs)
    paths = sorted(p for d in dirs for p in d.glob("*.jsonl"))
    for path in paths:
        try:
            events = list(read_events(path))
        except (OSError, UnicodeDecodeError, LedgerError):
            out.broken.append(path.stem)
            continue
        if not events:  # emptied after it was signed, or a crash before its first event
            if not sidecar(path).exists():
                out.incomplete.append(path.stem)
            elif verify_attestation(path, pubkey).status is Attestation.FORGED:
                out.forged.append(path.stem)
            else:
                out.broken.append(path.stem)
            continue
        if not verify_events(events).ok:
            out.broken.append(events[0].run_id)
            continue
        run_id = events[0].run_id
        last = events[-1]
        status = verify_attestation(path, pubkey).status
        if status is Attestation.FORGED:
            out.forged.append(run_id)
            continue
        if pubkey is not None and status is Attestation.UNATTESTED and last.kind is Kind.RUN_END:
            out.unsigned.append(run_id)  # an open one is signed only when it ends
            continue
        out.runs += 1
        principal = str(events[0].attrs.get("principal.id") or "unknown")
        person = people.person(principal)
        if person != principal:
            seen[person].add(principal)
        who = out.by_principal.setdefault(person, Usage())
        who.runs += 1
        requested: dict[str, str] = {}  # model.request id -> model asked for
        models_used: set[str] = set()
        for e in events:
            if e.kind is Kind.MODEL_REQUEST:
                requested[e.id] = str(e.attrs.get("gen_ai.request.model"))
            elif e.kind is Kind.MODEL_RESPONSE:
                if e.attrs.get("compliance.provenance"):
                    continue  # imported, but not a verified model answer: a marker or claim
                model = str(e.attrs.get("gen_ai.response.model") or "unknown")
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
        if last.kind is not Kind.RUN_END:
            out.incomplete.append(run_id)
        elif last.attrs.get("run.ok") is False:
            out.failed.append(run_id)
        if status is Attestation.UNATTESTED:
            out.unattested.append(run_id)
    for d in dirs:
        erased = erased_hashes(d)
        for side in sorted(d.glob("*.attest.json")):
            ledger = side.with_name(side.name.removesuffix(".attest.json") + ".jsonl")
            if not ledger.exists() and not erased_sidecar(side, erased):
                out.missing.append(ledger.stem)
    out.by_tool = dict(sorted(tools.items()))
    out.people = {person: sorted(ids) for person, ids in sorted(seen.items())}
    return out
