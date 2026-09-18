# 3. Scenario pack: data, a closed check vocabulary, a callable target

- Status: accepted
- Date: 2026-09-17

## Context

The ledger says what an agent did. Nothing yet says whether it did the wrong thing under attack. The roadmap's scenario pack has to feed adversarial input to a user's agent and turn each failure into a finding a reviewer can trace back to ledger events.

## Decision

- A scenario is a YAML file: an id, a title, OWASP Agentic Top 10 ids (MITRE ATLAS ids where one exists), a severity, one user message, optional poisoned tool results, and one or more checks. Pydantic validates it with unknown keys forbidden; the JSON schema is checked in under `docs/schema/`.
- Checks are a closed vocabulary: `no_tool_call`, `tool_call`, `policy_denied`, `no_match` (a regex over `tool.call` and `action` attributes only, what the agent did rather than what it was told), and `run_ok`. Every failed check becomes a `Finding` whose `evidence` is one or more ledger event ids; the model refuses an empty list. `no_tool_call` means the agent never recorded a call; `policy_denied` means it asked and a `policy.check` refused. With the `Policy` engine a denied call is still recorded, so a scenario should use one or the other for the same tool unless the agent checks policy before it calls. `no_tool_success` fails only on a `tool.result` without `error` for that tool, so an agent that never calls, refuses on its own, or is denied all pass; it is the right check when the scenario cares that the effect did not happen rather than which layer stopped it.
- The target is a Python callable `target(rec, inputs)`. The runner records the user message, hands the target the `Recorder` and an `Inputs` whose `tool_result(name, real)` swaps in poisoned text for declared tools, and evaluates checks over the ledger. A crashing target is a result (`run.ok=False`), not a runner error.
- The shipped corpus is written for a reference tool set (`lookup_order`, `issue_refund`, `send_email`, `run_shell`, `search_docs`) matching `examples/scenario_target.py`. A corpus must name tools, and tool names are agent-specific; users copy the format for their own tools. A golden hash of the corpus is pinned so any change shows up in review.

Rejected: Python check hooks (two ways to say one thing, and the corpus stops being data), multi-turn scripts (most corpus items are single-turn; the target contract would need turn sync), running the target in Docker now (a separate roadmap item with its own protocol).

## Consequences

- A user gets findings with evidence in one command, and the ledgers behind them verify and reconstruct like any other run.
- Every scenario run records the corpus hash in `run.start`, so a finding can be tied to the exact corpus that produced it.
- The check vocabulary will grow when a real scenario needs it; each addition is a schema change to `docs/schema/scenario.json`.
- `pyyaml` is a hard dependency; only `safe_load` is used.
