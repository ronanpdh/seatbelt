"""The recording gateway: authenticate, forward, relay, record."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any, cast

import anyio
import httpx2
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from seatbelt import __version__
from seatbelt.gateway.config import GatewayConfig, Principal
from seatbelt.gateway.formats import Format, anthropic, openai_chat
from seatbelt.gateway.formats.anthropic import AnthropicFormat
from seatbelt.gateway.formats.openai_chat import OpenAIChatFormat
from seatbelt.gateway.sessions import Session, Sessions
from seatbelt.ledger.events import Event
from seatbelt.policy.engine import Policy, denylist, max_output_tokens, models
from seatbelt.record.recorder import ModelCall

RUN_HEADER = "x-seatbelt-run"
RUN_END_HEADER = "x-seatbelt-run-end"
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
# accept-encoding: httpx2 decodes the body, so let it ask only for encodings it can decode
_STRIP_REQUEST = _HOP | {
    "host",
    "content-length",
    "accept-encoding",
    "authorization",
    "x-api-key",
    RUN_HEADER,
    RUN_END_HEADER,
}
# the body is relayed decoded, so the upstream's encoding and length no longer describe it
_STRIP_RESPONSE = _HOP | {"content-encoding", "content-length"}
_FORMATS: dict[str, tuple[str, str, type[Format]]] = {  # path -> (upstream, auth header, format)
    "/v1/messages": ("anthropic", "x-api-key", AnthropicFormat),
    "/v1/chat/completions": ("openai", "authorization", OpenAIChatFormat),
}
_ASSEMBLE: dict[str, Callable[[list[dict[str, Any]]], dict[str, Any]]] = {
    "anthropic": anthropic.assemble_sse,
    "openai": openai_chat.assemble_sse,
}

type Finish = Callable[[ModelCall, dict[str, Any] | None, str | None], None]


def _principal(cfg: GatewayConfig, request: Request) -> Principal | None:
    """Claude Code can send both headers, one of them another credential (an `apiKeyHelper`
    key in `x-api-key`, say), so each is tried."""
    bearer = request.headers.get("authorization", "")
    for raw in (
        request.headers.get("x-api-key", ""),
        bearer[7:] if bearer[:7].lower() == "bearer " else "",
    ):
        if raw.strip() and (principal := cfg.lookup(raw.strip())) is not None:
            return principal
    return None


def _error(status: int, kind: str, message: str) -> Response:
    """Shaped like Anthropic's errors; OpenAI clients read the same `error.message`."""
    return JSONResponse(
        {"type": "error", "error": {"type": kind, "message": message}}, status_code=status
    )


def _unauthorized() -> Response:
    return _error(401, "authentication_error", "unknown seatbelt key")


def _policies(cfg: GatewayConfig) -> tuple[Policy | None, Policy | None]:
    """(request policy, tool policy). None when unset, so policy-free ledgers stay unchanged."""
    p = cfg.policy
    rules = [
        *([models(*p.models)] if p.models is not None else []),
        *([max_output_tokens(p.max_output_tokens)] if p.max_output_tokens is not None else []),
    ]
    return (
        Policy(*rules) if rules else None,
        Policy(denylist(*p.tools_denied)) if p.tools_denied else None,
    )


def _upstream_headers(request: Request, auth_header: str, real_key: str) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST}
    headers[auth_header] = f"Bearer {real_key}" if auth_header == "authorization" else real_key
    return headers


def _relay_headers(resp: httpx2.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if k.lower() not in _STRIP_RESPONSE}


def _json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        value: Any = json.loads(raw)  # provider JSON
    except ValueError:
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _meta(request: Request) -> dict[str, Any]:
    return {
        "client.ip": request.client.host if request.client else None,
        "client.user_agent": request.headers.get("user-agent"),
        "gateway.version": __version__,
    }


def _locked[T](session: Session, fn: Callable[[], T]) -> T:
    with session.lock:  # one request at a time touches a session's chain
        return fn()


def _format(session: Session, name: str, cls: type[Format]) -> Format:
    fmt = session.formats.get(name)
    if fmt is None:
        fmt = session.formats[name] = cls(session.rec)
    return fmt


def _check_tool_call(session: Session, policy: Policy, event: Event) -> None:
    """Record the verdict on a tool call the model asked for. The gateway cannot stop a local
    tool, so the call is relayed; a denied call's result is refused on the way back."""
    name = str(event.attrs.get("gen_ai.tool.name"))
    arguments = event.attrs.get("gen_ai.tool.call.arguments")
    args = cast(dict[str, Any], arguments) if isinstance(arguments, dict) else {}
    for rule, reason in policy.evaluate(name, args):
        session.rec.policy_check(rule, event.id, reason is None, reason or "allowed")
        if reason:
            session.denied_calls[str(event.attrs.get("gen_ai.tool.call.id"))] = name


def _refusal(
    session: Session,
    fmt: Format,
    body: dict[str, Any],
    request_id: str,
    request_policy: Policy | None,
    tool_policy: Policy | None,
) -> str | None:
    """Record the checks on a request; return the first denial, or None. A tool result is
    refused if this session denied its call, or if the history names a denied tool (a new
    session after the idle window still carries the old conversation)."""
    if tool_policy is not None:
        for call_id, name in fmt.tool_result_calls(body):
            denied = session.denied_calls.get(call_id)
            if (
                denied is None
                and name is not None
                and any(r for _, r in tool_policy.evaluate(name, {}))
            ):
                denied = name
            if denied is not None:
                reason = f"tool result for denied call {call_id} ({denied})"
                session.rec.policy_check("denylist", request_id, False, reason)
                return reason
    if request_policy is None:
        return None
    first: str | None = None
    for rule, reason in request_policy.evaluate(fmt.model(body), body):
        session.rec.policy_check(rule, request_id, reason is None, reason or "allowed")
        first = first or reason
    return first


def sse_events(raw: bytes) -> list[dict[str, Any]]:
    """The JSON objects in an SSE body. Never raises: a broken stream must still be recorded."""
    out: list[dict[str, Any]] = []
    for block in raw.decode(errors="replace").replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
        )
        if not data or data == "[DONE]":
            continue
        try:
            value: Any = json.loads(data)  # provider JSON
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(cast(dict[str, Any], value))
    return out


class _RelayStream(StreamingResponse):
    """Runs `on_close` however the response ends: finished, upstream broke, or client left.
    Starlette skips `background` on a disconnect and an abandoned generator's `finally` runs
    only when it is collected, so neither can be trusted to release the session."""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        status_code: int,
        headers: dict[str, str],
        on_close: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(content, status_code=status_code, headers=headers)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._on_close()


def _stream(
    resp: httpx2.Response,
    client: httpx2.AsyncClient,
    call: ModelCall,
    provider: str,
    finish: Finish,
    settle: Callable[[], Awaitable[None]],
) -> Response:
    received = bytearray()
    outcome: list[str | None] = ["stream ended early"]  # cleared when the upstream completes

    async def relay() -> AsyncGenerator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                received.extend(chunk)
                yield chunk
        except httpx2.HTTPError as exc:
            outcome[0] = f"upstream stream broke: {type(exc).__name__}: {exc}"
            raise  # the client must see a broken stream, not a clean end
        outcome[0] = None

    chunks = relay()

    async def on_close() -> None:
        try:
            await chunks.aclose()
            await resp.aclose()
            await client.aclose()
        finally:
            assembled = _ASSEMBLE[provider](sse_events(bytes(received)))
            await run_in_threadpool(finish, call, assembled, outcome[0])
            await settle()

    return _RelayStream(chunks, resp.status_code, _relay_headers(resp), on_close)


def create_app(
    cfg: GatewayConfig, sessions: Sessions, transport: httpx2.AsyncBaseTransport | None = None
) -> Starlette:
    request_policy, tool_policy = _policies(cfg)

    async def relay(request: Request) -> Response:
        principal = _principal(cfg, request)
        if principal is None:
            return _unauthorized()
        upstream_name, auth_header, format_cls = _FORMATS[request.url.path]
        upstream = cfg.upstreams.get(upstream_name)
        if upstream is None:
            return _error(404, "not_found_error", f"no {upstream_name} upstream")
        raw = await request.body()
        body = _json_object(raw)
        if body is None:
            return _error(400, "invalid_request_error", "body must be a JSON object")
        run = request.headers.get(RUN_HEADER) or None
        run_end = request.headers.get(RUN_END_HEADER, "").lower() == "true"
        meta = {**_meta(request), "principal.key_id": principal.key_sha256[:12]}  # which issued key
        session = await run_in_threadpool(sessions.get, principal.id, run, meta)
        call: ModelCall | None = None
        handed_off = False  # a stream settles the session itself when it closes
        refused = False  # policy answered the request; no model ever saw it

        def fmt() -> Format:  # under the session lock, so parallel requests share one
            return _format(session, upstream_name, format_cls)

        def finish(call: ModelCall, payload: dict[str, Any] | None, error: str | None) -> None:
            def run() -> None:
                for event in fmt().finish(call, payload, error=error):
                    if tool_policy is not None:
                        _check_tool_call(session, tool_policy, event)

            _locked(session, run)

        def admit() -> tuple[ModelCall, str | None]:
            """Record the request, then the policy verdicts on it. Returns a refusal reason."""
            f = fmt()
            call = f.begin(body)
            refusal = _refusal(session, f, body, call.request.id, request_policy, tool_policy)
            return call, refusal

        async def settle() -> None:
            """Answer a dangling call, end the run if asked, release the session."""
            if call is not None and call.response is None and not refused:
                await run_in_threadpool(finish, call, None, "request did not complete")
            if run_end:
                await run_in_threadpool(sessions.end, principal.id, run)
            await run_in_threadpool(sessions.release, session)

        try:
            call, refusal = await run_in_threadpool(_locked, session, admit)
            if refusal is not None:
                refused = True
                return _error(403, "permission_error", refusal)
            client = httpx2.AsyncClient(
                base_url=upstream.url, transport=request.app.state.transport, timeout=600
            )
            outgoing = client.build_request(
                "POST",
                request.url.path,
                content=raw,
                headers=_upstream_headers(
                    request, auth_header, os.environ.get(upstream.key_env, "")
                ),
            )
            try:
                resp = await client.send(outgoing, stream=True)
                streaming = resp.status_code < 400 and resp.headers.get(
                    "content-type", ""
                ).startswith("text/event-stream")
                if not streaming:
                    await resp.aread()
            except httpx2.HTTPError as exc:
                await client.aclose()
                error = f"upstream unreachable: {type(exc).__name__}: {exc}"
                await run_in_threadpool(finish, call, None, error)
                return _error(502, "api_error", error)
            if streaming:
                handed_off = True
                return _stream(resp, client, call, upstream_name, finish, settle)
            await client.aclose()
            payload = _json_object(resp.content)
            if resp.status_code >= 400:
                error: str | None = f"{resp.status_code}: {resp.text[:200]}"
                payload = None
            else:
                error = None if payload is not None else f"{resp.status_code}: not a JSON object"
            await run_in_threadpool(finish, call, payload, error)
            return Response(
                resp.content, status_code=resp.status_code, headers=_relay_headers(resp)
            )
        finally:
            if not handed_off:
                # shielded: a cancelled or failed request must still answer its call and
                # release its session, or the chain keeps a dangling request and never closes
                with anyio.CancelScope(shield=True):
                    await settle()

    async def forward(request: Request) -> Response:
        """Probes a client makes besides inference: authenticated and forwarded, not recorded.
        A token count or a model list is nothing an auditor needs."""
        principal = _principal(cfg, request)
        if principal is None:
            return _unauthorized()
        anthropic_style = request.url.path.startswith("/v1/messages") or (
            "x-api-key" in request.headers
        )
        name, auth_header = (
            ("anthropic", "x-api-key") if anthropic_style else ("openai", "authorization")
        )
        upstream = cfg.upstreams.get(name)
        if upstream is None:
            return _error(404, "not_found_error", f"no {name} upstream")
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        async with httpx2.AsyncClient(
            base_url=upstream.url, transport=request.app.state.transport, timeout=60
        ) as client:
            try:
                resp = await client.request(
                    request.method,
                    path,
                    content=await request.body(),
                    headers=_upstream_headers(
                        request, auth_header, os.environ.get(upstream.key_env, "")
                    ),
                )
            except httpx2.HTTPError as exc:
                return _error(
                    502, "api_error", f"upstream unreachable: {type(exc).__name__}: {exc}"
                )
        return Response(resp.content, status_code=resp.status_code, headers=_relay_headers(resp))

    async def hello(_: Request) -> Response:
        return Response(status_code=200)

    async def end_run(request: Request) -> Response:
        principal = _principal(cfg, request)
        if principal is None:
            return _unauthorized()
        name = request.path_params["name"]
        found = await run_in_threadpool(sessions.end, principal.id, name)
        return Response(status_code=204 if found else 404)

    app = Starlette(
        routes=[
            Route("/v1/messages", relay, methods=["POST"]),
            Route("/v1/chat/completions", relay, methods=["POST"]),
            Route("/v1/messages/count_tokens", forward, methods=["POST"]),
            Route("/v1/models", forward, methods=["GET"]),
            Route("/v1/models/{id:path}", forward, methods=["GET"]),
            Route("/api/hello", hello, methods=["GET", "HEAD"]),
            Route("/seatbelt/runs/{name}/end", end_run, methods=["POST"]),
        ]
    )
    app.state.transport = transport  # read per request so tests can swap upstreams
    return app
