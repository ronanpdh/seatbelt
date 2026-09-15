"""OpenAI Agents SDK adapter: a tracing processor.

Usage::

    agents.add_trace_processor(SeatbeltProcessor(rec))
"""

from __future__ import annotations

import json
import threading
from typing import Any, cast

from agents.tracing import (
    AgentSpanData,
    FunctionSpanData,
    GenerationSpanData,
    GuardrailSpanData,
    HandoffSpanData,
    ResponseSpanData,
    Span,
    Trace,
    TracingProcessor,
)

from seatbelt.record.recorder import Recorder


class SeatbeltProcessor(TracingProcessor):
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self._lock = threading.Lock()  # SDK may end spans from several threads; seq must not race
        self._agents: dict[str, tuple[str, str]] = {}  # span_id -> (agent name, decision id)
        self._last_response: dict[str, str] = {}  # trace_id -> response event id

    def attach(self, rec: Recorder) -> None:
        with self._lock:
            self._rec = rec
            self._agents.clear()
            self._last_response.clear()

    def on_trace_start(self, trace: Trace) -> None:
        pass

    def on_trace_end(self, trace: Trace) -> None:
        with self._lock:
            self._last_response.pop(trace.trace_id, None)

    def on_span_start(self, span: Span[Any]) -> None:
        data = span.span_data
        if not isinstance(data, AgentSpanData):
            return
        # Recorded at start so the authority precedes the actions it covers.
        with self._lock:
            event = self._rec.decision(f"agent {data.name} takes control", data.name, [])
            self._agents[span.span_id] = (data.name, event.id)

    def on_span_end(self, span: Span[Any]) -> None:
        data = span.span_data
        with self._lock:
            match data:
                case GenerationSpanData():
                    request = {"input": data.input, "model_config": data.model_config}
                    self._model_call(
                        span, data.model or "unknown", request, data.output, data.usage
                    )
                case ResponseSpanData():
                    response = data.response
                    model = response.model if response else None
                    output = response.output if response else None
                    request = {"input": data.input}
                    self._model_call(span, model or "unknown", request, output, data.usage, model)
                case FunctionSpanData():
                    mcp = {f"mcp.{k}": v for k, v in (data.mcp_data or {}).items()}
                    call = self._rec.tool_called(
                        data.name,
                        _arguments(data.input),
                        attrs=mcp,
                        parent_id=self._last_response.get(span.trace_id),
                    )
                    error = span.error
                    self._rec.tool_returned(call, data.output, error["message"] if error else None)
                case HandoffSpanData():
                    name, basis = self._parent_agent(span, data.from_agent)
                    self._rec.decision(f"handoff {data.from_agent} -> {data.to_agent}", name, basis)
                case GuardrailSpanData():
                    name, basis = self._parent_agent(span, None)
                    outcome = "triggered" if data.triggered else "passed"
                    self._rec.decision(f"guardrail {data.name} {outcome}", name, basis)
                case _:
                    pass

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass

    def _model_call(
        self,
        span: Span[Any],
        model: str,
        request: dict[str, Any],
        output: Any,
        usage: dict[str, Any] | None,
        response_model: str | None = None,
    ) -> None:
        tokens = {k: v for k, v in (usage or {}).items() if isinstance(v, int)}
        with self._rec.model_call(model, request, provider="openai") as call:
            answer = call.respond({"output": output}, tokens, response_model=response_model)
        self._last_response[span.trace_id] = answer.id  # function spans don't name their generation

    def _parent_agent(self, span: Span[Any], fallback: str | None) -> tuple[str, list[str]]:
        name, decision_id = self._agents.get(span.parent_id or "", (fallback or "unknown", ""))
        return name, [decision_id] if decision_id else []


def _arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {"value": parsed}
