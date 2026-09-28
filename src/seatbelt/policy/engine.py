"""Rules evaluated at the tool boundary, each recorded as a `policy.check` event.

Usage::

    policy = Policy(allowlist("lookup_order", "refund"), Rule("cap", lambda tool, args: ...))
    with Recorder.start(root, agent_id="bot", policy=policy) as rec:
        with rec.tool_call("refund", {"amount": 500}) as tool:   # raises PolicyDenied first
            ...
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from seatbelt.ledger.events import Event

Check = Callable[[str, dict[str, Any]], str | None]  # (tool, arguments) -> reason to deny, or None


@dataclass(frozen=True)
class Rule:
    name: str
    check: Check


class PolicyDenied(Exception):
    def __init__(self, rule: str, reason: str, call: Event) -> None:
        super().__init__(f"{rule}: {reason}")
        self.rule = rule
        self.reason = reason
        self.call = call


class Policy:
    def __init__(self, *rules: Rule) -> None:
        self.rules = rules

    def evaluate(self, tool: str, arguments: dict[str, Any]) -> list[tuple[str, str | None]]:
        """Every rule's verdict, in order: (rule name, reason to deny or None). A rule that
        raises is a denial, so a buggy rule fails closed and still leaves a record."""
        verdicts: list[tuple[str, str | None]] = []
        for rule in self.rules:
            try:
                reason = rule.check(tool, arguments)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            verdicts.append((rule.name, reason))
        return verdicts


def allowlist(*tools: str) -> Rule:
    return Rule("allowlist", lambda tool, _: None if tool in tools else f"{tool} is not allowed")


def denylist(*tools: str) -> Rule:
    return Rule("denylist", lambda tool, _: f"{tool} is denied" if tool in tools else None)


def models(*allowed: str) -> Rule:
    """For a model request: the first argument is the model name. Exact match."""
    return Rule(
        "models", lambda model, _: None if model in allowed else f"model {model} is not allowed"
    )


def max_output_tokens(limit: int) -> Rule:
    """For a model request: caps `max_tokens` (Anthropic, OpenAI), `max_completion_tokens`
    (OpenAI Chat Completions), `max_output_tokens` (OpenAI Responses) and
    `generationConfig.maxOutputTokens` (Gemini). A request that sets none passes; the
    provider's default applies."""

    def check(_: str, args: dict[str, Any]) -> str | None:
        caps = {
            k: args.get(k) for k in ("max_tokens", "max_completion_tokens", "max_output_tokens")
        }
        generation: Any = args.get("generationConfig")
        if isinstance(generation, dict):
            value: Any = cast(dict[str, Any], generation).get("maxOutputTokens")
            caps["generationConfig.maxOutputTokens"] = value
        for key, n in caps.items():
            if isinstance(n, int) and n > limit:
                return f"{key} {n} exceeds {limit}"
        return None

    return Rule("max_output_tokens", check)
