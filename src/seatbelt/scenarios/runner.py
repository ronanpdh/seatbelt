"""Run every scenario in a corpus against a target and collect findings."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from seatbelt.attest.manifest import sidecar
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder
from seatbelt.scenarios.checks import Finding, evaluate
from seatbelt.scenarios.model import Inputs, ScenarioError, corpus_sha256, load_corpus

if TYPE_CHECKING:
    from seatbelt.attest.sign import Signer
    from seatbelt.policy.engine import Policy

Target = Callable[[Recorder, Inputs], None]
"""Drives the agent under test. The runner has already recorded the user message."""


class ScenarioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    ledger: str
    run_ok: bool
    findings: int


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus_sha256: str
    results: list[ScenarioResult]
    findings: list[Finding]

    @property
    def passed(self) -> bool:
        return not self.findings


def run(
    corpus: Path,
    target: Target,
    out: Path,
    *,
    agent_id: str = "target",
    policy: Policy | None = None,
    signer: Signer | None = None,
) -> Report:
    scenarios = load_corpus(corpus)
    taken = [
        s.id
        for s in scenarios
        if (path := out / f"{s.id}.jsonl").exists()
        or (signer is not None and sidecar(path).exists())
    ]
    if taken:
        raise ScenarioError(f"{out} already holds ledgers for {', '.join(taken)}")
    digest = corpus_sha256(corpus)
    results: list[ScenarioResult] = []
    findings: list[Finding] = []
    for s in scenarios:
        meta = {"scenario.id": s.id, "scenario.owasp": s.owasp, "corpus.sha256": digest}
        path = out / f"{s.id}.jsonl"
        try:
            with Recorder.start(
                out, agent_id=agent_id, run_id=s.id, metadata=meta, policy=policy, signer=signer
            ) as rec:
                rec.user_message("scenario", s.user_message)
                target(rec, Inputs(s.user_message, dict(s.tool_results)))
        except Exception as exc:  # a crashing target is a result (run.ok=False), not a runner error
            if not path.exists():
                raise ScenarioError(
                    f"{path}: recorder failed before the target ran: {exc}"
                ) from exc
        events = list(read_events(path))
        if not events or events[0].kind is not Kind.RUN_START:
            raise ScenarioError(f"{path}: no run.start recorded")
        found = evaluate(s, events)
        ok = any(e.kind is Kind.RUN_END and e.attrs.get("run.ok") is True for e in events)
        results.append(
            ScenarioResult(scenario_id=s.id, ledger=str(path), run_ok=ok, findings=len(found))
        )
        findings.extend(found)
    report = Report(corpus_sha256=digest, results=results, findings=findings)
    (out / "findings.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return report
