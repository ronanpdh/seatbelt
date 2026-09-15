import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import Ledger
from seatbelt.record.recorder import Recorder
from seatbelt.verify.chain import verify_file

agents = pytest.importorskip("agents")
from agents.tracing import (  # noqa: E402
    AgentSpanData,
    FunctionSpanData,
    GenerationSpanData,
    HandoffSpanData,
    ResponseSpanData,
    Span,
    SpanData,
)
from openai.types.responses import Response  # noqa: E402

from seatbelt.adapters.openai_agents import SeatbeltProcessor  # noqa: E402

FIXTURE = Path(__file__).parent.parent / "fixtures" / "openai_refund.json"


@dataclass
class FakeSpan:
    span_data: SpanData
    span_id: str
    parent_id: str | None = None
    trace_id: str = "trace_1"
    error: None = None


def span(
    data: SpanData, span_id: str, parent_id: str | None = None, trace_id: str = "trace_1"
) -> Span[Any]:
    return cast(Span[Any], FakeSpan(data, span_id, parent_id, trace_id))


@pytest.fixture
def run(tmp_path: Path) -> Iterator[tuple[Recorder, Path]]:
    with Recorder.start(tmp_path, agent_id="refund-bot", run_id="r") as rec:
        yield rec, tmp_path / "r.jsonl"


def events(path: Path) -> list[Event]:
    return list(Ledger(path, path.stem).read())


def test_generation_and_function_spans(run: tuple[Recorder, Path]) -> None:
    rec, path = run
    proc = SeatbeltProcessor(rec)
    proc.on_span_end(
        span(
            GenerationSpanData(
                input=[{"role": "user", "content": "Refund order 1001"}],
                output=[{"role": "assistant", "tool_calls": [{"name": "lookup_order"}]}],
                model="gpt-5",
                model_config={"temperature": 0},
                usage={"input_tokens": 12, "output_tokens": 4, "input_tokens_details": {}},
            ),
            "span_gen",
        )
    )
    proc.on_span_end(
        span(
            FunctionSpanData(
                "lookup_order", '{"order_id": "1001"}', {"status": "delivered"}, {"server": "shop"}
            ),
            "span_fn",
        )
    )
    rec.outcome("done", success=True)

    evs = events(path)
    assert [e.kind for e in evs[1:5]] == [
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,
    ]
    req, resp, call, result = evs[1:5]
    assert req.attrs["gen_ai.request.model"] == "gpt-5"
    assert req.attrs["gen_ai.provider.name"] == "openai"
    assert req.attrs["gen_ai.request"]["model_config"] == {"temperature": 0}
    assert resp.parent_id == req.id
    assert resp.attrs["gen_ai.usage.output_tokens"] == 4
    assert "gen_ai.usage.input_tokens_details" not in resp.attrs
    assert call.attrs["gen_ai.tool.call.arguments"] == {"order_id": "1001"}
    assert call.attrs["mcp.server"] == "shop"
    assert call.parent_id == resp.id
    assert result.parent_id == call.id
    assert result.attrs["gen_ai.tool.call.result"] == {"status": "delivered"}


def test_response_span_records_provider_model_version(run: tuple[Recorder, Path]) -> None:
    rec, path = run
    response = Response.model_validate(json.loads(FIXTURE.read_text())[1])
    data = ResponseSpanData(response=response, input="Refund order 1001")
    data.usage = {"input_tokens": 30, "output_tokens": 9}
    SeatbeltProcessor(rec).on_span_end(span(data, "span_resp"))

    req, resp = events(path)[1:3]
    assert req.attrs["gen_ai.request.model"] == response.model
    assert resp.actor.version == response.model
    assert resp.attrs["gen_ai.usage.output_tokens"] == 9


def test_agent_and_handoff_are_decisions_in_the_authority_chain(
    run: tuple[Recorder, Path],
) -> None:
    rec, path = run
    proc = SeatbeltProcessor(rec)
    triage = span(AgentSpanData("triage"), "span_triage")
    proc.on_span_start(triage)
    proc.on_span_end(triage)
    proc.on_span_end(span(HandoffSpanData("triage", "refunds"), "span_h", "span_triage"))

    agent, handoff = [e for e in events(path) if e.kind == Kind.DECISION]
    assert agent.attrs["decision.authority"] == "triage"
    assert handoff.attrs["decision.authority"] == "triage"
    assert "refunds" in handoff.attrs["decision.summary"]
    assert handoff.attrs["decision.basis"] == [agent.id]


def test_concurrent_traces_link_tools_to_their_own_response(
    run: tuple[Recorder, Path],
) -> None:
    rec, path = run
    proc = SeatbeltProcessor(rec)
    proc.on_span_end(span(GenerationSpanData(model="a"), "g1", trace_id="t1"))
    proc.on_span_end(span(GenerationSpanData(model="b"), "g2", trace_id="t2"))
    proc.on_span_end(span(FunctionSpanData("tool_a", "{}", "x"), "f1", trace_id="t1"))

    evs = events(path)
    response_a = next(
        e for e in evs if e.kind == Kind.MODEL_RESPONSE and e.attrs["gen_ai.response.model"] == "a"
    )
    call = next(e for e in evs if e.kind == Kind.TOOL_CALL)
    assert call.parent_id == response_a.id


def test_bad_tool_input_does_not_raise(run: tuple[Recorder, Path]) -> None:
    rec, path = run
    proc = SeatbeltProcessor(rec)
    proc.on_span_end(span(FunctionSpanData("t", None, "x"), "a"))
    proc.on_span_end(span(FunctionSpanData("t", "not json", "x"), "b"))
    calls = [e for e in events(path) if e.kind == Kind.TOOL_CALL]
    assert calls[0].attrs["gen_ai.tool.call.arguments"] == {}
    assert calls[1].attrs["gen_ai.tool.call.arguments"] == {"raw": "not json"}


def test_attach_swaps_the_recorder(tmp_path: Path) -> None:
    with Recorder.start(tmp_path, agent_id="a", run_id="one") as first:
        proc = SeatbeltProcessor(first)
    with Recorder.start(tmp_path, agent_id="a", run_id="two") as second:
        proc.attach(second)
        proc.on_span_end(span(FunctionSpanData("t", "{}", "x"), "s"))
    assert Kind.TOOL_CALL not in [e.kind for e in events(tmp_path / "one.jsonl")]
    assert Kind.TOOL_CALL in [e.kind for e in events(tmp_path / "two.jsonl")]


def test_real_runner_matches_anthropic_ledger_shape(tmp_path: Path) -> None:
    from agents import Agent, OpenAIResponsesModel, Runner, function_tool, set_trace_processors
    from agents.tracing.processors import default_processor
    from openai import AsyncOpenAI

    replies = iter(Response.model_validate(r) for r in json.loads(FIXTURE.read_text()))

    async def create(**_: Any) -> Response:
        return next(replies)

    client = AsyncOpenAI(api_key="test", base_url="http://127.0.0.1:9")
    client.responses.create = create  # type: ignore[method-assign]

    @function_tool
    def lookup_order(order_id: str) -> str:
        """Look up an order by id."""
        return json.dumps({"status": "delivered", "total": 49.0})

    agent = Agent(
        name="refund-agent",
        tools=[lookup_order],
        model=OpenAIResponsesModel(model="gpt-5", openai_client=client),
    )
    with Recorder.start(tmp_path, agent_id="refund-agent", run_id="oa") as rec:
        set_trace_processors([SeatbeltProcessor(rec)])  # no SDK exporter, no network
        try:
            Runner.run_sync(agent, "Refund order 1001")
        finally:
            set_trace_processors([default_processor()])

    anthropic_shape = [
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,
        Kind.MODEL_REQUEST,
        Kind.MODEL_RESPONSE,
    ]
    path = tmp_path / "oa.jsonl"
    kinds = [e.kind for e in events(path)]
    assert kinds[:2] == [Kind.RUN_START, Kind.DECISION]
    assert [k for k in kinds if k in set(anthropic_shape)] == anthropic_shape
    assert verify_file(path).ok
