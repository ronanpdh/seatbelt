"""Regression tests for the gateway review's fixes: probe paths, forwarded headers, body
limits, OIDC key fetching, the output-token cap, logs, stream recording and closing ledgers."""

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tests.helpers import sse

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, keygen
from seatbelt.gateway import app as app_mod
from seatbelt.gateway import serve as serve_mod
from seatbelt.gateway.app import Live, create_app
from seatbelt.gateway.config import (
    GatewayConfig,
    OidcConfig,
    Principal,
    Upstream,
    add_principal,
    key_hash,
)
from seatbelt.gateway.formats.anthropic import AnthropicFormat
from seatbelt.gateway.oidc import (
    JWKS_GRACE,
    JWKS_LIFESPAN,
    OidcError,
    OidcUnavailable,
    OidcVerifier,
)
from seatbelt.gateway.sessions import CLOSING, Sessions, close_open_chains
from seatbelt.ledger.events import Actor, ActorType, Event, Kind
from seatbelt.ledger.store import Ledger, read_events
from seatbelt.locks import try_lock
from seatbelt.policy.engine import max_output_tokens
from seatbelt.verify.chain import verify_file

KEY = "sbk_review-key-for-tests"
REAL = {  # fake provider keys the gateway injects
    "ANTHROPIC_API_KEY": "sk-ant-REAL",
    "OPENAI_API_KEY": "sk-REAL",
    "GEMINI_API_KEY": "AIza-REAL",
}


@dataclass
class Gw:
    client: TestClient
    app: Starlette
    ledgers: Path
    seen: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def upstream(self, reply: Callable[[httpx2.Request], httpx2.Response]) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            self.seen.append(request)
            return reply(request)

        self.app.state.transport = httpx2.MockTransport(handler)

    def events(self) -> list[Event]:
        return [e for p in sorted(self.ledgers.glob("*.jsonl")) for e in read_events(p)]


def _cfg(tmp_path: Path, **extra: Any) -> GatewayConfig:
    """The org's key for Anthropic, OpenAI and Gemini; Code Assist passes the client's on."""
    return GatewayConfig(
        ledgers=tmp_path / "runs",
        upstreams={
            "anthropic": Upstream(url="https://api.anthropic.com", key_env="ANTHROPIC_API_KEY"),
            "openai": Upstream(url="https://api.openai.com", key_env="OPENAI_API_KEY"),
            "gemini": Upstream(
                url="https://generativelanguage.googleapis.com", key_env="GEMINI_API_KEY"
            ),
            "codeassist": Upstream(url="https://cloudcode-pa.googleapis.com"),
        },
        principals=[Principal(id="me", key_sha256=key_hash(KEY), issued=date.today())],
        **extra,
    )


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gw]:
    for name, value in REAL.items():
        monkeypatch.setenv(name, value)
    cfg = _cfg(tmp_path)
    sessions = Sessions(cfg.ledgers, Signer.generate(), idle=900)
    app = create_app(cfg, sessions, path_credentials=True)  # as the local recorder runs it
    with TestClient(app) as client:
        gw = Gw(client, app, cfg.ledgers)
        gw.upstream(lambda _: httpx2.Response(200, json={"ok": 1}))
        yield gw
    assert sessions.close_all(timeout=1) == 0


OPENAI = {"authorization": f"Bearer {KEY}"}
GOOGLE = {"x-goog-api-key": KEY}
MESSAGE = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}


# -- GW-1, GW-2: GET probes reach only the resource they name ---------------------------


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/v1/models/%2e%2e/files", OPENAI),
        ("/v1/models/..%2Ffiles", OPENAI),
        ("/v1/models/gpt-5%2F..%2F..%2Ffiles", OPENAI),
        ("/v1/models/%2e/gpt-5", OPENAI),
        ("/v1/models/%2e%2e/messages/batches/b1/results", {"x-api-key": KEY}),
        ("/v1beta/models/%2e%2e/files", GOOGLE),
        ("/v1beta/models/%2e%2e/cachedContents", GOOGLE),
        ("/v1alpha/models/%2e%2e/tunedModels", GOOGLE),
        # GW-2: a colon names a method, which a lookup must not reach
        ("/v1beta/models/gemini-2.5-pro:generateContent", GOOGLE),
        ("/v1/models/gemini-2.5-pro:generateContent", GOOGLE),
        (f"/v1/models/gemini-2.5-pro:generateContent?key={KEY}", {}),
        ("/v1internal/x/%2e%2e/%2e%2e/v1internal:generateContent", OPENAI),
        ("/v1internal/operations/%2e%2e/%2e%2e/v1internal:generateContent", OPENAI),
        ("/v1internal/projects/p1", OPENAI),
        ("/v1internal/operations/op1:cancel", OPENAI),
    ],
)
def test_a_probe_cannot_reach_another_upstream_path(
    gw: Gw, path: str, headers: dict[str, str]
) -> None:
    r = gw.client.get(path, headers=headers)
    assert r.status_code == 400 and gw.seen == []


def test_every_legitimate_probe_still_goes_through(gw: Gw) -> None:
    probes = [
        ("/v1/models/gpt-5", OPENAI),
        ("/v1/models/ft:gpt-4o-mini-2024-07-18:acme::abc123", OPENAI),
        ("/v1/models/claude-sonnet-4-5", {"x-api-key": KEY, "anthropic-version": "1"}),
        ("/v1beta/models/gemini-2.5-pro", GOOGLE),
        ("/v1alpha/models/gemini-2.5-pro?pageSize=5", GOOGLE),
        ("/v1/models/gemini-2.5-pro", GOOGLE),
        ("/v1internal/operations/onboard-abc_123.x", {"authorization": "Bearer ya29.x", **OPENAI}),
    ]
    for path, headers in probes:
        assert gw.client.get(path, headers=headers).status_code == 200, path
    assert gw.client.head("/v1/models/gpt-5", headers=OPENAI).status_code == 200
    assert [str(r.url) for r in gw.seen] == [
        "https://api.openai.com/v1/models/gpt-5",
        "https://api.openai.com/v1/models/ft:gpt-4o-mini-2024-07-18:acme::abc123",
        "https://api.anthropic.com/v1/models/claude-sonnet-4-5",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro",
        "https://generativelanguage.googleapis.com/v1alpha/models/gemini-2.5-pro?pageSize=5",
        "https://generativelanguage.googleapis.com/v1/models/gemini-2.5-pro",
        "https://cloudcode-pa.googleapis.com/v1internal/operations/onboard-abc_123.x",
        "https://api.openai.com/v1/models/gpt-5",
    ]
    assert gw.seen[-1].method == "HEAD"


def test_a_get_or_head_sends_no_body_upstream(gw: Gw) -> None:
    for method in ("GET", "HEAD"):
        r = gw.client.request(
            method, "/v1beta/models/gemini-2.5-pro", content=b'{"contents": []}', headers=GOOGLE
        )
        assert r.status_code == 200
    assert [r.content for r in gw.seen] == [b"", b""]
    assert [r.headers.get("content-length", "0") for r in gw.seen] == ["0", "0"]


def test_method_override_headers_are_never_forwarded(gw: Gw) -> None:
    overrides = {"X-HTTP-Method-Override": "POST", "X-HTTP-Method": "POST"}
    overrides["X-Method-Override"] = "DELETE"
    gw.client.get("/v1/models/gpt-5", headers={**OPENAI, **overrides})
    gw.client.post("/v1/messages", json=MESSAGE, headers={"x-api-key": KEY, **overrides})
    gw.client.post(  # pass-through upstream
        "/v1internal:loadCodeAssist", json={}, headers={**OPENAI, **overrides}
    )
    assert len(gw.seen) == 3
    for request in gw.seen:
        assert not {k.lower() for k in overrides} & {k.lower() for k in request.headers}


# -- GW-10: with the org's key, no header picks the org's project ----------------------


def test_project_headers_are_dropped_with_the_orgs_key_and_kept_when_passed_through(
    gw: Gw,
) -> None:
    routing = {
        "OpenAI-Organization": "org-other",
        "OpenAI-Project": "proj_finance",
        "x-goog-user-project": "other-project",
    }
    gw.client.post("/v1/chat/completions", json=MESSAGE, headers={**OPENAI, **routing})
    gw.client.get("/v1beta/models/gemini-2.5-pro", headers={**GOOGLE, **routing})
    org_key = gw.seen[:2]
    gw.client.post(
        "/v1internal:loadCodeAssist",
        json={},
        headers={"authorization": "Bearer ya29.own", "x-seatbelt-key": KEY, **routing},
    )
    for request in org_key:
        assert not {k.lower() for k in routing} & {k.lower() for k in request.headers}
    passed = gw.seen[2].headers
    assert passed["openai-project"] == "proj_finance"
    assert passed["x-goog-user-project"] == "other-project"
    assert passed["authorization"] == "Bearer ya29.own"


# -- GW-5: request bodies are bounded ---------------------------------------------------


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(app_mod, "_MAX_BODY", 1000)
    return 1000


def _big(size: int) -> bytes:
    return b'{"model": "m", "max_tokens": 5, "messages": [], "pad": "' + b"x" * size + b'"}'


def test_an_oversized_body_is_refused_before_it_is_recorded_or_sent(gw: Gw, small: int) -> None:
    body = _big(small)
    headers = {"x-api-key": KEY, "content-type": "application/json"}
    by_length = gw.client.post("/v1/messages", content=body, headers=headers)
    assert by_length.status_code == 413
    assert by_length.json()["error"]["type"] == "request_too_large"
    chunked = gw.client.post(
        "/v1/messages", content=iter([body[:600], body[600:]]), headers=headers
    )
    assert chunked.status_code == 413
    count = gw.client.post("/v1/messages/count_tokens", content=body, headers=headers)
    assert count.status_code == 413
    method = "/v1beta/models/gemini-2.5-pro:countTokens"
    assert gw.client.post(method, content=body, headers=GOOGLE).status_code == 413
    assert gw.seen == [] and gw.events() == []
    ok = gw.client.post("/v1/messages", content=_big(small // 2), headers=headers)
    assert ok.status_code == 200


def test_an_identity_body_past_the_limit_is_not_decoded(small: int) -> None:
    assert app_mod._decoded(b"x" * small, "") == b"x" * small  # pyright: ignore[reportPrivateUsage]
    assert app_mod._decoded(b"x" * (small + 1), "identity") is None  # pyright: ignore[reportPrivateUsage]


# -- GW-6, GW-12: OIDC keys ---------------------------------------------------------------

ISSUER = "https://login.example.com/tenant/v2.0"
AUDIENCE = "app-1"
JWKS_URL = "https://login.example.com/tenant/keys"


class Idp:
    """A provider whose key set can be made to fail, or to hang until released."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.calls = 0
        self.failing = False
        self.gate: threading.Event | None = None

    def fetch(self, url: str) -> dict[str, Any]:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.failing:
            raise OidcUnavailable(f"{url}: ConnectTimeout('internal egress 10.0.0.9')")
        jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": "k1", "alg": "RS256"}]}

    def token(self) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1", "iat": now, "exp": now + 600}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})


def _verifier(idp: Idp, now: list[float]) -> OidcVerifier:
    cfg = OidcConfig(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL)
    return OidcVerifier(cfg, idp.fetch, clock=lambda: now[0])


def test_a_failed_key_fetch_keeps_the_held_keys_for_a_grace_period(
    caplog: pytest.LogCaptureFixture,
) -> None:
    idp, now = Idp(), [1000.0]
    verifier = _verifier(idp, now)
    assert verifier.verify(idp.token())["sub"] == "user-1"
    idp.failing = True
    now[0] += JWKS_LIFESPAN + 1
    with caplog.at_level(logging.WARNING, logger="seatbelt.gateway.oidc"):
        assert verifier.verify(idp.token())["sub"] == "user-1"  # the held keys
    assert idp.calls == 2 and "ConnectTimeout" in caplog.text
    now[0] += 1
    verifier.verify(idp.token())
    assert idp.calls == 2  # a failure is not retried on every request
    now[0] += 30
    verifier.verify(idp.token())
    assert idp.calls == 3  # but after the cooldown
    now[0] = 1000.0 + JWKS_LIFESPAN + JWKS_GRACE + 1
    with pytest.raises(OidcUnavailable):
        verifier.verify(idp.token())  # past the grace period the old keys are not used


def test_a_failed_first_fetch_obeys_the_cooldown() -> None:
    idp, now = Idp(), [1000.0]
    idp.failing = True
    verifier = _verifier(idp, now)
    for _ in range(3):
        with pytest.raises(OidcUnavailable):
            verifier.verify(idp.token())
    assert idp.calls == 1
    idp.failing = False
    now[0] += 31
    assert verifier.verify(idp.token())["sub"] == "user-1"


def test_one_thread_fetches_while_the_others_use_the_held_keys() -> None:
    idp, now = Idp(), [1000.0]
    verifier = _verifier(idp, now)
    verifier.verify(idp.token())
    now[0] += JWKS_LIFESPAN + 1
    idp.gate = threading.Event()
    fetcher = threading.Thread(target=verifier.verify, args=(idp.token(),))
    fetcher.start()
    while idp.calls < 2:
        time.sleep(0.005)
    started = time.monotonic()
    assert verifier.verify(idp.token())["sub"] == "user-1"  # not behind the hung fetch
    assert time.monotonic() - started < 2 and idp.calls == 2
    idp.gate.set()
    fetcher.join(5)


def test_a_key_fetch_failure_tells_the_client_nothing_about_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in REAL.items():
        monkeypatch.setenv(name, value)
    cfg = _cfg(tmp_path, oidc=OidcConfig(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL))
    sessions = Sessions(cfg.ledgers, None, idle=900)
    app = create_app(cfg, sessions)
    idp = Idp()
    idp.failing = True
    live: Live = app.state.live
    assert live.oidc is not None
    app.state.live = Live(
        live.cfg, live.request_policy, live.tool_policy, OidcVerifier(live.oidc.cfg, idp.fetch)
    )
    with TestClient(app) as client:
        r = client.post(
            "/v1/messages", json=MESSAGE, headers={"authorization": f"Bearer {idp.token()}"}
        )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "sign-in token refused"


# -- GW-8: the output-token cap reads every number a provider might ---------------------


@pytest.mark.parametrize(
    ("args", "denied"),
    [
        ({"max_tokens": 500}, False),
        ({"max_tokens": 500.0}, False),
        ({"max_tokens": "500"}, False),
        ({"max_tokens": None}, False),
        ({}, False),
        ({"max_completion_tokens": 60000.0}, True),
        ({"max_output_tokens": "60000"}, True),
        ({"max_tokens": " 60000 "}, True),
        ({"generationConfig": {"maxOutputTokens": "60000"}}, True),
        ({"max_tokens": 6e4}, True),
        # not a whole number: denied rather than ignored
        ({"max_tokens": True}, True),
        ({"max_tokens": 1.5}, True),
        ({"max_tokens": "6e4"}, True),
        ({"max_tokens": "lots"}, True),
        ({"max_tokens": [1]}, True),
        ({"generationConfig": {"maxOutputTokens": {"n": 1}}}, True),
    ],
)
def test_the_output_token_cap_reads_numbers_in_any_form(args: dict[str, Any], denied: bool) -> None:
    assert (max_output_tokens(1000).check("m", args) is not None) is denied


# -- GW-9: no keys in the gateway's logs --------------------------------------------------


def test_the_access_log_filter_redacts_keys() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "10.0.0.1:5000",
            "POST",
            f"/_seatbelt/{KEY}/run-1/v1beta/models/m:generateContent?alt=sse&key={KEY}&%24key={KEY}",
            "1.1",
            200,
        ),
        None,
    )
    assert serve_mod._RedactKeys().filter(record)  # pyright: ignore[reportPrivateUsage]
    message = record.getMessage()
    assert KEY not in message
    assert "/_seatbelt/REDACTED/run-1/v1beta/models/m:generateContent?alt=sse" in message
    assert message.endswith('&key=REDACTED&%24key=REDACTED HTTP/1.1" 200')


def test_path_credentials_are_off_in_gateway_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keygen(tmp_path / "keys")
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "signing_key: keys/seatbelt.key\nledgers: runs\n"
        "upstreams:\n  anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}\n"
    )
    key = add_principal(path, "alice@corp")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REAL")
    answers: list[int] = []
    seen: list[httpx2.Request] = []

    def fake_run(app: Starlette, **_: object) -> None:
        def reply(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={})

        app.state.transport = httpx2.MockTransport(reply)
        with TestClient(app) as client:
            r = client.post(f"/_seatbelt/{key}/run-1/v1/messages", json=MESSAGE)
            answers.append(r.status_code)
            r = client.post("/v1/messages", json=MESSAGE, headers={"x-api-key": key})
            answers.append(r.status_code)

    access, loud = logging.getLogger("uvicorn.access"), logging.getLogger("httpx2")
    filters, level = list(access.filters), loud.level
    monkeypatch.setattr(serve_mod.uvicorn, "run", fake_run)
    try:
        serve_mod.serve(path)
        assert any(isinstance(f, serve_mod._RedactKeys) for f in access.filters)  # pyright: ignore[reportPrivateUsage]
        assert loud.level == logging.WARNING
    finally:
        access.filters[:] = filters
        loud.setLevel(level)
    assert answers == [404, 200] and len(seen) == 1


def test_path_credentials_still_work_for_the_local_recorder(gw: Gw) -> None:
    r = gw.client.post(
        f"/_seatbelt/{KEY}/run-1/v1/messages",
        json=MESSAGE,
        headers={"x-api-key": "sk-ant-own"},
    )
    assert r.status_code == 200 and gw.events()[0].attrs["run.name"] == "run-1"


# -- FSR-12, LC-1: recording a stream -------------------------------------------------------

SSE = {"content-type": "text/event-stream"}
REPLY: dict[str, Any] = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "streamed answer"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 2},
}


def test_a_stream_is_assembled_off_the_event_loop(gw: Gw, monkeypatch: pytest.MonkeyPatch) -> None:
    on_loop: list[bool] = []
    parse = app_mod.sse_events

    def watched(raw: bytes) -> list[dict[str, Any]]:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return parse(raw)

    monkeypatch.setattr(app_mod, "sse_events", watched)
    gw.upstream(lambda _: httpx2.Response(200, headers=SSE, content=sse(REPLY)))
    body = {**MESSAGE, "stream": True}
    r = gw.client.post("/v1/messages", json=body, headers={"x-api-key": KEY})
    assert r.status_code == 200 and on_loop == [False]
    assert gw.events()[-1].attrs["gen_ai.response"]["content"][0]["text"] == "streamed answer"


def test_a_stream_that_cannot_be_recorded_is_kept_as_received(
    gw: Gw, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(self: AnthropicFormat, call: Any, response: Any, error: Any = None) -> list[Event]:
        raise ValueError("cannot serialise this response")

    monkeypatch.setattr(AnthropicFormat, "finish", broken)
    gw.upstream(lambda _: httpx2.Response(200, headers=SSE, content=sse(REPLY)))
    body = {**MESSAGE, "stream": True}
    with caplog.at_level(logging.ERROR, logger="seatbelt.gateway.app"):
        r = gw.client.post("/v1/messages", json=body, headers={"x-api-key": KEY})
    assert r.status_code == 200 and "streamed answer" in r.text
    response = gw.events()[-1]
    assert response.kind is Kind.MODEL_RESPONSE
    assert "cannot serialise this response" in response.attrs["error"]
    kept = response.attrs["gen_ai.response"]
    assert "streamed answer" in kept["unassembled_sse"]
    assert kept["received_bytes"] == len(sse(REPLY))
    assert "recording a streamed response failed" in caplog.text


# -- LC-3: one ledger failing to close does not strand the others --------------------------


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def _fail_close(monkeypatch: pytest.MonkeyPatch, sessions: list[Any]) -> None:
    def fail() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(sessions[0].stack, "close", fail)


def _closed(path: Path) -> bool:
    return list(read_events(path))[-1].kind is Kind.RUN_END


@pytest.mark.parametrize("how", ["sweep", "end_principal", "close_all"])
def test_one_failed_close_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, how: str
) -> None:
    clock = Clock()
    sessions = Sessions(tmp_path, None, idle=60, clock=clock)
    opened = [sessions.get("alice@corp", f"run-{i}", {}) for i in range(3)]
    for s in opened:
        sessions.release(s)
    _fail_close(monkeypatch, opened)
    clock.now += 61
    with caplog.at_level(logging.ERROR, logger="seatbelt.gateway.sessions"):
        if how == "sweep":
            assert sessions.sweep() == 3
        elif how == "end_principal":
            assert sessions.end_principal("alice@corp") == 3
        else:
            assert sessions.close_all(timeout=1) == 0
    assert [_closed(s.rec.ledger.path) for s in opened] == [False, True, True]
    assert "disk full" in caplog.text


# -- CLS-3: closing a dead ledger once, whoever else is closing it --------------------------


def _open_ledger(root: Path, run: str = "killed") -> Path:
    """The ledger of a run whose process was killed: started, never closed."""
    ledger = Ledger(root / f"{run}.jsonl", run)
    ledger.append(Kind.RUN_START, Actor(type=ActorType.AGENT, id="gateway"), {"run.name": run})
    ledger.append(Kind.USER_MESSAGE, Actor(type=ActorType.USER, id="u"), {"text": "hello"})
    return ledger.path


def test_a_ledger_closed_meanwhile_is_not_closed_again(tmp_path: Path) -> None:
    path = _open_ledger(tmp_path)
    signer = Signer.generate()
    others: list[list[Path]] = []

    def another_process_closes_it_first(_: list[Event]) -> bool:
        others.append(close_open_chains(tmp_path, signer))
        return True

    assert close_open_chains(tmp_path, signer, only=another_process_closes_it_first) == []
    assert others == [[path]]
    ends = [e for e in read_events(path) if e.kind is Kind.RUN_END]
    assert len(ends) == 1 and verify_file(path).complete and sidecar(path).exists()
    assert list((tmp_path / CLOSING).iterdir()) == []  # no lock file left behind


def test_a_ledger_another_process_is_closing_is_left_to_it(tmp_path: Path) -> None:
    path = _open_ledger(tmp_path)
    before = path.read_bytes()
    (tmp_path / CLOSING).mkdir()
    with (tmp_path / CLOSING / f"{path.name}.lock").open("wb") as held:
        assert try_lock(held)
        assert close_open_chains(tmp_path, Signer.generate()) == []
    assert path.read_bytes() == before
    assert close_open_chains(tmp_path, Signer.generate()) == [path]


def test_racing_closers_close_each_ledger_once(tmp_path: Path) -> None:
    paths = [_open_ledger(tmp_path, f"run-{i}") for i in range(12)]
    signer = Signer.generate()
    start = threading.Barrier(3)
    results: list[list[Path]] = []
    errors: list[BaseException] = []

    def closer() -> None:
        start.wait()
        try:
            results.append(close_open_chains(tmp_path, signer))
        except BaseException as exc:  # an AttestError from a second closer, say
            errors.append(exc)

    threads = [threading.Thread(target=closer) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert sorted(p for r in results for p in r) == sorted(paths)  # each by one closer
    for path in paths:
        assert [e.kind for e in read_events(path)].count(Kind.RUN_END) == 1
        assert verify_file(path).complete


def test_claim_errors_keep_their_message() -> None:
    """Claim errors keep PyJWT's message, which tells a user what to fix."""
    idp, now = Idp(), [time.time()]
    verifier = _verifier(idp, now)
    token = jwt.encode(
        {"iss": ISSUER, "aud": "another-app", "sub": "u", "exp": int(time.time()) + 60},
        idp.key,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(OidcError, match=r"(?i)audience") as info:
        verifier.verify(token)
    assert not isinstance(info.value, OidcUnavailable)
