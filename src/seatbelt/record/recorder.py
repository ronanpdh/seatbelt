"""Recorder: the API adapters call. Owns actor identities and parent links."""

from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from seatbelt import __version__
from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, attest
from seatbelt.ledger.events import Actor, ActorType, Event, Kind
from seatbelt.ledger.redact import redact
from seatbelt.ledger.store import Ledger
from seatbelt.policy.engine import PolicyDenied

if TYPE_CHECKING:
    from seatbelt.policy.engine import Policy

_RUN_ID = re.compile(r"[A-Za-z0-9_.-]+")


class Recorder:
    """Records one run (one conversation or task) between a user and an agent.

    Usage::

        with Recorder.start(Path("runs"), agent_id="support-bot") as rec:
            rec.user_message("u-42", "Refund order 1001")
            with rec.model_call("claude-sonnet-4-5", {"messages": [...]}) as call:
                call.respond({"content": "..."}, usage={"input_tokens": 12})
            with rec.tool_call("refund", {"order": 1001}) as tool:
                tool.result({"status": "ok"})
            rec.outcome("refund issued", success=True)
    """

    def __init__(self, ledger: Ledger, agent: Actor, policy: Policy | None = None) -> None:
        self.ledger = ledger
        self.agent = agent
        self.policy = policy
        self.run_id = ledger.run_id

    @classmethod
    @contextmanager
    def start(
        cls,
        root: Path,
        agent_id: str,
        agent_version: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        policy: Policy | None = None,
        signer: Signer | None = None,
    ) -> Generator[Recorder]:
        run_id = run_id or uuid4().hex
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError(f"run_id {run_id!r} must match {_RUN_ID.pattern}")
        path = root / f"{run_id}.jsonl"
        if path.exists() or (signer is not None and sidecar(path).exists()):
            raise FileExistsError(f"{path} or its attestation already exists")
        ledger = Ledger(path, run_id)
        agent = Actor(type=ActorType.AGENT, id=agent_id, version=agent_version)
        rec = cls(ledger, agent, policy)
        rec._emit(
            Kind.RUN_START,
            Actor(type=ActorType.SYSTEM, id="seatbelt", version=__version__),
            {"harness.version": __version__, **(metadata or {})},
        )
        error: str | None = None
        try:
            yield rec
        except BaseException as exc:
            error = _describe(exc)
            raise
        finally:
            rec._emit(
                Kind.RUN_END,
                agent,
                {"run.ok": error is None, "run.error": error, "run.events": ledger.length + 1},
            )
            if signer is not None:
                attest(path, signer)  # a signed failure is still evidence

    # -- primitives ---------------------------------------------------------

    def _emit(
        self,
        kind: Kind,
        actor: Actor,
        attrs: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> Event:
        return self.ledger.append(kind, actor, redact(attrs or {}), parent_id)

    def user_message(self, user_id: str, content: str) -> Event:
        return self._emit(
            Kind.USER_MESSAGE,
            Actor(type=ActorType.USER, id=user_id),
            {"gen_ai.input.messages": [{"role": "user", "content": content}]},
        )

    def model_requested(
        self, model: str, request: dict[str, Any], provider: str | None = None
    ) -> ModelCall:
        """Record the request now; the caller answers with `ModelCall.respond` later."""
        req = self._emit(
            Kind.MODEL_REQUEST,
            self.agent,
            {
                "gen_ai.request.model": model,
                "gen_ai.provider.name": provider,
                "gen_ai.request": request,
            },
        )
        return ModelCall(self, req, model)

    @contextmanager
    def model_call(
        self, model: str, request: dict[str, Any], provider: str | None = None
    ) -> Generator[ModelCall]:
        call = self.model_requested(model, request, provider)
        try:
            yield call
        except BaseException as exc:
            if call.response is None:
                call.respond({}, error=_describe(exc))
            raise

    def tool_called(
        self,
        name: str,
        arguments: dict[str, Any],
        call_id: str | None = None,
        attrs: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> Event:
        """Record that the agent asked for a tool. `parent_id` is the model response that
        requested it, when the adapter knows it."""
        return self._emit(
            Kind.TOOL_CALL,
            self.agent,
            {
                "gen_ai.tool.name": name,
                "gen_ai.tool.call.id": call_id,
                "gen_ai.tool.call.arguments": arguments,
                **(attrs or {}),
            },
            parent_id=parent_id,
        )

    def tool_returned(self, call: Event, result: Any, error: str | None = None) -> Event:
        """Record the tool's answer, linked to the call it answers."""
        return self._emit(
            Kind.TOOL_RESULT,
            Actor(type=ActorType.TOOL, id=str(call.attrs.get("gen_ai.tool.name"))),
            {"gen_ai.tool.call.result": result, "error": error},
            parent_id=call.id,
        )

    @contextmanager
    def tool_call(self, name: str, arguments: dict[str, Any]) -> Generator[ToolCall]:
        """Record the call, check it against the policy (raises `PolicyDenied`), then run it.
        A body that raises before `result` gets an error `tool.result`, like `model_call`."""
        call = self.tool_called(name, arguments)
        if self.policy is not None:
            denied: PolicyDenied | None = None
            for rule, reason in self.policy.evaluate(name, arguments):
                self.policy_check(rule, call.id, reason is None, reason or "allowed")
                if reason and denied is None:
                    denied = PolicyDenied(rule, reason, call)
            if denied:
                raise denied
        tool = ToolCall(self, call, name)
        try:
            yield tool
        except BaseException as exc:
            if tool.answer is None:
                tool.result(None, error=_describe(exc))
            raise

    def policy_check(self, policy: str, subject_id: str, allowed: bool, reason: str) -> Event:
        return self._emit(
            Kind.POLICY_CHECK,
            Actor(type=ActorType.POLICY, id=policy),
            {"policy.allowed": allowed, "policy.reason": reason},
            parent_id=subject_id,
        )

    def decision(self, summary: str, authority: str, basis: list[str]) -> Event:
        """A choice the agent made, who was accountable, and which event ids justify it."""
        return self._emit(
            Kind.DECISION,
            self.agent,
            {"decision.summary": summary, "decision.authority": authority, "decision.basis": basis},
        )

    def action(self, description: str, target: str, decision_id: str | None = None) -> Event:
        return self._emit(
            Kind.ACTION,
            self.agent,
            {"action.description": description, "action.target": target},
            parent_id=decision_id,
        )

    def outcome(self, summary: str, success: bool) -> Event:
        return self._emit(
            Kind.OUTCOME, self.agent, {"outcome.summary": summary, "outcome.success": success}
        )


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class ModelCall:
    def __init__(self, rec: Recorder, request: Event, model: str) -> None:
        self._rec = rec
        self.request = request
        self.model = model
        self.response: Event | None = None

    def respond(
        self,
        response: dict[str, Any],
        usage: dict[str, int] | None = None,
        response_model: str | None = None,
        error: str | None = None,
    ) -> Event:
        """`response_model` is the exact version string the provider returned, if any."""
        self.response = self._rec._emit(  # pyright: ignore[reportPrivateUsage]
            Kind.MODEL_RESPONSE,
            Actor(type=ActorType.MODEL, id=self.model, version=response_model),
            {
                "gen_ai.response.model": response_model or self.model,
                "gen_ai.response": response,
                "error": error,
                **{f"gen_ai.usage.{k}": v for k, v in (usage or {}).items()},
            },
            parent_id=self.request.id,
        )
        return self.response


class ToolCall:
    def __init__(self, rec: Recorder, call: Event, name: str) -> None:
        self._rec = rec
        self.call = call
        self.name = name
        self.answer: Event | None = None

    def result(self, result: Any, error: str | None = None) -> Event:
        self.answer = self._rec.tool_returned(self.call, result, error)
        return self.answer
