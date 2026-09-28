"""The recording gateway: authenticate, forward, relay, record."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, cast

import anyio
import httpx2
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from seatbelt import __version__
from seatbelt.gateway.config import GatewayConfig, Principal
from seatbelt.gateway.formats import Format
from seatbelt.gateway.formats.anthropic import AnthropicFormat
from seatbelt.gateway.formats.openai_chat import OpenAIChatFormat
from seatbelt.gateway.sessions import Session, Sessions
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


def _principal(cfg: GatewayConfig, request: Request) -> Principal | None:
    raw = request.headers.get("x-api-key") or request.headers.get("authorization", "")
    key = raw[7:] if raw[:7].lower() == "bearer " else raw
    return cfg.lookup(key.strip()) if key.strip() else None


def _unauthorized() -> Response:
    return JSONResponse({"error": "unknown seatbelt key"}, status_code=401)


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


def create_app(
    cfg: GatewayConfig, sessions: Sessions, transport: httpx2.AsyncBaseTransport | None = None
) -> Starlette:
    async def relay(request: Request) -> Response:
        principal = _principal(cfg, request)
        if principal is None:
            return _unauthorized()
        upstream_name, auth_header, format_cls = _FORMATS[request.url.path]
        upstream = cfg.upstreams.get(upstream_name)
        if upstream is None:
            return JSONResponse({"error": f"no {upstream_name} upstream"}, status_code=404)
        raw = await request.body()
        body = _json_object(raw)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        run = request.headers.get(RUN_HEADER) or None
        session = await run_in_threadpool(sessions.get, principal.id, run, _meta(request))
        call: ModelCall | None = None

        def fmt() -> Format:  # under the session lock, so parallel requests share one
            return _format(session, upstream_name, format_cls)

        def finish(call: ModelCall, payload: dict[str, Any] | None, error: str | None) -> None:
            _locked(session, lambda: fmt().finish(call, payload, error=error))

        try:
            call = await run_in_threadpool(_locked, session, lambda: fmt().begin(body))
            try:
                async with httpx2.AsyncClient(
                    base_url=upstream.url, transport=request.app.state.transport, timeout=600
                ) as client:
                    resp = await client.post(
                        request.url.path,
                        content=raw,
                        headers=_upstream_headers(
                            request, auth_header, os.environ.get(upstream.key_env, "")
                        ),
                    )
            except httpx2.HTTPError as exc:
                error = f"upstream unreachable: {type(exc).__name__}: {exc}"
                await run_in_threadpool(finish, call, None, error)
                return JSONResponse({"error": error}, status_code=502)
            payload = _json_object(resp.content)
            if resp.status_code >= 400:
                error: str | None = f"{resp.status_code}: {resp.text[:200]}"
                payload = None
            else:
                error = None if payload is not None else f"{resp.status_code}: not a JSON object"
            await run_in_threadpool(finish, call, payload, error)
            return Response(resp.content, status_code=resp.status_code, headers=_relay_headers(resp))
        finally:
            # shielded: a cancelled or failed request must still answer its call and release
            # its session, or the chain is left with a dangling request and never closes
            with anyio.CancelScope(shield=True):
                if call is not None and call.response is None:
                    await run_in_threadpool(finish, call, None, "request did not complete")
                if request.headers.get(RUN_END_HEADER, "").lower() == "true":
                    await run_in_threadpool(sessions.end, principal.id, run)
                await run_in_threadpool(sessions.release, session)

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
            Route("/seatbelt/runs/{name}/end", end_run, methods=["POST"]),
        ]
    )
    app.state.transport = transport  # read per request so tests can swap upstreams
    return app
