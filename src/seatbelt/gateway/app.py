"""The recording gateway: authenticate, forward, relay, record."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import zlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import unquote_plus

import anyio
import httpx2
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from seatbelt import __version__
from seatbelt.gateway.config import KEY_PREFIX, GatewayConfig, Principal, Upstream
from seatbelt.gateway.formats import (
    Format,
    anthropic,
    as_dicts,
    gemini,
    openai_chat,
    openai_responses,
)
from seatbelt.gateway.formats.anthropic import AnthropicFormat
from seatbelt.gateway.formats.gemini import CodeAssistFormat, GeminiFormat
from seatbelt.gateway.formats.openai_chat import OpenAIChatFormat
from seatbelt.gateway.formats.openai_responses import OpenAIResponsesFormat
from seatbelt.gateway.oidc import OidcError, OidcUnavailable, OidcVerifier, looks_like_jwt
from seatbelt.gateway.sessions import Session, Sessions
from seatbelt.ledger.events import Event
from seatbelt.policy.engine import Policy, denylist, max_output_tokens, models
from seatbelt.record.recorder import ModelCall

_log = logging.getLogger(__name__)

RUN_HEADER = "x-seatbelt-run"
RUN_END_HEADER = "x-seatbelt-run-end"
# the issued key, for a client whose own auth headers carry its provider credentials to a
# pass-through upstream (`key_env` unset): `seatbelt run` in local mode
KEY_HEADER = "x-seatbelt-key"
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
    "x-goog-api-key",
    RUN_HEADER,
    RUN_END_HEADER,
    KEY_HEADER,
    # an upstream that honours one would run another method than the one routed and recorded
    "x-http-method-override",
    "x-http-method",
    "x-method-override",
}
# with the gateway's provider key: headers that pick which of the org's projects, and so whose
# quota, billing and stored objects, that key acts on
_STRIP_WITH_KEY = _STRIP_REQUEST | {"openai-organization", "openai-project", "x-goog-user-project"}
_AUTH_HEADERS = frozenset({"authorization", "x-api-key", "x-goog-api-key"})
# to a pass-through upstream the client's own credentials go on as sent
_STRIP_PASSTHROUGH = _STRIP_REQUEST - _AUTH_HEADERS
# the body is relayed decoded, so the upstream's encoding and length no longer describe it
_STRIP_RESPONSE = _HOP | {"content-encoding", "content-length"}
type Assemble = Callable[[list[dict[str, Any]]], dict[str, Any]]
type Spec = tuple[str, str, type[Format], Assemble]  # upstream, auth header, format, assembler
# path -> how it is relayed and recorded
_FORMATS: dict[str, Spec] = {
    "/v1/messages": ("anthropic", "x-api-key", AnthropicFormat, anthropic.assemble_sse),
    "/v1/chat/completions": (
        "openai",
        "authorization",
        OpenAIChatFormat,
        openai_chat.assemble_sse,
    ),
    "/v1/responses": (
        "openai",
        "authorization",
        OpenAIResponsesFormat,
        openai_responses.assemble_sse,
    ),
}
# Gemini: POST /{version}/models/{model}:{method}. The model is in the path
_GEMINI: Spec = ("gemini", "x-goog-api-key", GeminiFormat, gemini.assemble_sse)
_GEMINI_VERSIONS = ("v1beta", "v1", "v1alpha")
_GEMINI_RECORDED = frozenset({"generateContent", "streamGenerateContent"})
# forwarded unrecorded, like Anthropic's count_tokens: nothing a model acts on comes back.
# Gemini CLI counts tokens for images and files; Google's SDKs embed with batchEmbedContents.
# Any other method is refused rather than let through unrecorded
_GEMINI_FORWARDED = frozenset({"countTokens", "embedContent", "batchEmbedContents"})
_GEMINI_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # e.g. gemini-2.5-pro
# a model looked up with GET: one path segment, never a dot segment. An OpenAI fine-tuned
# model's id has colons (ft:gpt-4o-mini:org::id); a Google-style id must match _GEMINI_MODEL,
# where a colon would name a method
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
# the query parameters Google reads as an API key (cloud.google.com/apis/docs/system-parameters)
_KEY_PARAMS = frozenset({"key", "$key"})
# Gemini CLI signed in with Google: POST /v1internal:{method} on Google's Code Assist service
# (gemini-cli packages/core/src/code_assist/server.ts), reached through CODE_ASSIST_ENDPOINT
_CODE_ASSIST: Spec = (
    "codeassist",
    "authorization",
    CodeAssistFormat,
    gemini.assemble_code_assist_sse,
)
_CODE_ASSIST_FORWARDED = frozenset(
    {
        "loadCodeAssist",
        "onboardUser",
        "retrieveUserQuota",
        "listExperiments",
        "fetchAdminControls",
        "getCodeAssistGlobalUserSetting",
        "setCodeAssistGlobalUserSetting",
        "countTokens",
        "recordCodeAssistMetrics",
    }
)
# GET /v1internal/{name}: the long-running operation Gemini CLI polls while onboarding
_CODE_ASSIST_OPERATION = re.compile(r"operations/[A-Za-z0-9][A-Za-z0-9._-]*")
# Codex signed in with ChatGPT sends its account id beside the token; its Responses traffic
# goes to ChatGPT's backend, not the public API (codex-rs model-provider-info, chatgpt base)
_CHATGPT_ACCOUNT = "chatgpt-account-id"
_CHATGPT_PREFIX = "/backend-api/codex"
_MAX_BODY = 64 * 1024 * 1024  # a request body larger than this, as sent or inflated, is refused
# of a stream the gateway could not record as a response, this much is kept in its place
_MAX_UNASSEMBLED = 1024 * 1024
# nesting of a request body's objects and arrays; the ledger's serialiser gives up near 255
_MAX_DEPTH = 128
# google.rpc.Code names, which Google's clients read beside the HTTP status. 502 has no code
# of its own; UNAVAILABLE is the nearest
_GOOGLE_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    502: "UNAVAILABLE",
}

type Finish = Callable[[ModelCall, dict[str, Any] | None, str | None], None]


@dataclass(frozen=True)
class Identity:
    """Who a request is from. `session_key` separates sessions: an issued key's hash, or for
    OIDC a hash of issuer and subject, which stays the same as the provider's tokens rotate."""

    id: str
    session_key: str
    attrs: dict[str, Any]  # recorded on run.start

    @classmethod
    def of_key(cls, principal: Principal) -> Identity:
        attrs = {"principal.key_id": principal.key_sha256[:12]}  # which issued key
        return cls(principal.id, principal.key_sha256, attrs)


def _oidc_identity(request: Request, verifier: OidcVerifier, token: str) -> Identity | Response:
    try:
        claims = verifier.verify(token)
    except OidcUnavailable:  # logged by the verifier; nothing about the provider goes out
        return _error(request, 401, "authentication_error", "sign-in token refused")
    except OidcError as exc:
        return _error(request, 401, "authentication_error", f"sign-in token refused: {exc}")
    cfg = verifier.cfg
    subject = claims.get(cfg.principal_claim)
    if not isinstance(subject, str) or not subject:
        return _error(
            request, 401, "authentication_error", f"sign-in token has no {cfg.principal_claim}"
        )
    if cfg.allow is not None and subject not in cfg.allow:
        return _error(
            request, 403, "permission_error", f"{subject} is not allowed to use this gateway"
        )
    name = claims.get(cfg.name_claim) if cfg.name_claim else None
    attrs: dict[str, Any] = {
        "principal.auth": "oidc",
        "principal.issuer": claims.get("iss"),
        **({"principal.name": name} if isinstance(name, str) else {}),
    }
    key = hashlib.sha256(f"oidc\0{claims.get('iss')}\0{subject}".encode()).hexdigest()
    return Identity(subject, key, attrs)


async def _identify(live: Live, request: Request) -> Identity | Response:
    """An issued key in any header a client puts its key in (Claude Code can send two, one of
    them another credential, e.g. an `apiKeyHelper` key in `x-api-key`; Google's clients send
    `x-goog-api-key`, or `?key=` by hand; beside provider credentials, `x-seatbelt-key`),
    else a provider's sign-in token as the Bearer (Claude Desktop with
    `inferenceGatewayOidc`). An error response otherwise."""
    bearer = request.headers.get("authorization", "")
    token = bearer[7:].strip() if bearer[:7].lower() == "bearer " else ""
    for raw in (
        request.headers.get(KEY_HEADER, "").strip(),
        request.headers.get("x-api-key", "").strip(),
        request.headers.get("x-goog-api-key", "").strip(),
        request.query_params.get("key", "").strip(),
        token,
    ):
        if raw and (principal := live.cfg.lookup(raw)) is not None:
            return Identity.of_key(principal)
    if live.oidc is not None and looks_like_jwt(token):
        # may fetch keys
        return await run_in_threadpool(_oidc_identity, request, live.oidc, token)
    return _unauthorized(request)


def _google_style(request: Request) -> bool:
    """A request from a Google client: a Gemini API path, or its key header or parameter."""
    path = request.url.path
    return (
        path.startswith(("/v1beta/", "/v1alpha/", "/v1internal"))
        # a method, POST /v1/models/{m}:{method}; a GET is a model lookup, and an OpenAI
        # fine-tuned model's id has colons in it (ft:gpt-4o-mini:org::id)
        or (request.method == "POST" and path.startswith("/v1/models/") and ":" in path)
        or "x-goog-api-key" in request.headers
        or "key" in request.query_params
    )


def _error(request: Request, status: int, kind: str, message: str) -> Response:
    """Shaped like Anthropic's errors, whose `error.message` OpenAI clients read too; for a
    Google client like Google's (`error.code`, `error.message`, `error.status`)."""
    if _google_style(request):
        error = {
            "code": status,
            "message": message,
            "status": _GOOGLE_STATUS.get(status, "UNKNOWN"),
        }
        return JSONResponse({"error": error}, status_code=status)
    return JSONResponse(
        {"type": "error", "error": {"type": kind, "message": message}}, status_code=status
    )


def _unauthorized(request: Request) -> Response:
    return _error(request, 401, "authentication_error", "unknown seatbelt key")


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


@dataclass(frozen=True)
class Live:
    """The config a request runs under, with the policies built from it. A reload replaces it
    whole and each request reads it once, so no request mixes two configs."""

    cfg: GatewayConfig
    request_policy: Policy | None
    tool_policy: Policy | None
    oidc: OidcVerifier | None = None

    @classmethod
    def of(cls, cfg: GatewayConfig, previous: Live | None = None) -> Live:
        """`previous`'s OIDC verifier, and so its fetched keys, is kept if unchanged."""
        if cfg.oidc is None:
            oidc = None
        elif previous is not None and previous.oidc is not None and previous.oidc.cfg == cfg.oidc:
            oidc = previous.oidc
        else:
            oidc = OidcVerifier(cfg.oidc)
        return cls(cfg, *_policies(cfg), oidc=oidc)


def _live(request: Request) -> Live:
    return cast(Live, request.app.state.live)


def _is_seatbelt_key(value: str) -> bool:
    """A credential that is an issued seatbelt key, bare or as a Bearer token. By prefix: a
    provider's own token can contain the same characters anywhere else."""
    value = value.strip()
    if value[:7].lower() == "bearer ":
        value = value[7:].strip()
    return value.startswith(KEY_PREFIX)


def _upstream_headers(request: Request, upstream: Upstream, auth_header: str) -> dict[str, str]:
    """The request's headers for the upstream: with the gateway's provider key in place of the
    client's, or for a pass-through upstream with the client's own credentials. A seatbelt
    key is never sent on, whatever header it came in."""
    if upstream.key_env is None:
        return {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _STRIP_PASSTHROUGH
            and not (k.lower() in _AUTH_HEADERS and _is_seatbelt_key(v))
        }
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_WITH_KEY}
    real_key = os.environ.get(upstream.key_env, "")
    headers[auth_header] = f"Bearer {real_key}" if auth_header == "authorization" else real_key
    return headers


def _relay_headers(resp: httpx2.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if k.lower() not in _STRIP_RESPONSE}


async def _body(request: Request) -> bytes | None:
    """The request body, or None when it is larger than `_MAX_BODY`: by its Content-Length
    before any of it is read, else as soon as that much has arrived."""
    length = request.headers.get("content-length", "").strip()
    if length.isdigit() and int(length) > _MAX_BODY:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_BODY:
            return None
    return bytes(body)


def _too_large(request: Request) -> Response:
    return _error(request, 413, "request_too_large", "request body too large")


def _decoded(raw: bytes, encoding: str) -> bytes | None:
    """A request body as sent before its content-encoding (gzip or deflate), or None for an
    encoding the gateway cannot read or a body past `_MAX_BODY`, as sent or inflated."""
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return raw if len(raw) <= _MAX_BODY else None
    wbits = {"gzip": 31, "x-gzip": 31, "deflate": 15}.get(encoding)
    if wbits is None:
        return None
    try:
        inflater = zlib.decompressobj(wbits)
        out = inflater.decompress(raw, _MAX_BODY)
    except zlib.error:
        return None
    return out if not inflater.unconsumed_tail else None


def _route(cfg: GatewayConfig, request: Request, name: str) -> tuple[str, str]:
    """(upstream name, path prefix to put in place of /v1) for a request meant for `name`: a
    Codex signed in with ChatGPT goes to ChatGPT's backend when a `chatgpt` upstream is set."""
    if name == "openai" and "chatgpt" in cfg.upstreams and request.headers.get(_CHATGPT_ACCOUNT):
        return "chatgpt", _CHATGPT_PREFIX
    return name, ""


def _routed(path: str, prefix: str) -> str:
    return prefix + path.removeprefix("/v1") if prefix else path


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)  # provider JSON
    except ValueError:
        return None


def _json_object(raw: bytes) -> dict[str, Any] | None:
    value = _json(raw)
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _deeper_than(value: Any, limit: int) -> bool:
    """Whether JSON `value` nests objects and arrays more than `limit` deep. Walked with a
    stack, not recursion, so a hostile body cannot exhaust the interpreter's."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: list[Any] = list(cast(dict[str, Any], node).values())
        elif isinstance(node, list):
            children = cast(list[Any], node)
        else:
            continue
        if depth >= limit:
            return True
        stack.extend((c, depth + 1) for c in children)
    return False


def _with_query(request: Request, upstream: Upstream, path: str) -> str:
    """`path` and the query to send upstream, less any key parameter: a seatbelt key given as
    `?key=` goes no further than the gateway, and a client's own provider key is not used
    unless the upstream passes the client's credentials through (a seatbelt key never is)."""

    def kept(param: str) -> bool:
        name, _, value = param.partition("=")
        if unquote_plus(name) not in _KEY_PARAMS:
            return True
        return upstream.key_env is None and not _is_seatbelt_key(unquote_plus(value))

    query = "&".join(p for p in request.url.query.split("&") if p and kept(p))
    return path + (f"?{query}" if query else "")


def _meta(request: Request) -> dict[str, Any]:
    return {
        "client.ip": request.client.host if request.client else None,
        "client.user_agent": request.headers.get("user-agent"),
        "gateway.version": __version__,
    }


def _locked[T](session: Session, fn: Callable[[], T]) -> T:
    with session.lock:  # one request at a time touches a session's chain
        return fn()


def _format(session: Session, cls: type[Format]) -> Format:
    """One per format and session: Chat Completions and Responses share an upstream but not
    their open tool calls."""
    fmt = session.formats.get(cls.__name__)
    if fmt is None:
        fmt = session.formats[cls.__name__] = cls(session.rec)
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
    refused while the policy denies the tool this session recorded for its call, or the tool
    the history names (a new session after the idle window still carries the old
    conversation). A result held back earlier is let through, and that recorded, once a
    config reload stops denying its tool."""
    if tool_policy is not None:
        for call_id, name in fmt.tool_result_calls(body):
            remembered = session.denied_calls.get(call_id)
            denied = next(
                (
                    tool
                    for tool in dict.fromkeys(t for t in (remembered, name) if t)
                    if any(r for _, r in tool_policy.evaluate(tool, {}))
                ),
                None,
            )
            if denied is not None:
                reason = f"tool result for denied call {call_id} ({denied})"
                session.rec.policy_check("denylist", request_id, False, reason)
                return reason
            if remembered is not None:  # denied when called; the policy has changed since
                reason = f"tool result for call {call_id} ({remembered}): no longer denied"
                session.rec.policy_check("denylist", request_id, True, reason)
    if request_policy is None:
        return None
    first: str | None = None
    # a format that wraps the request (Code Assist) gives the policy what is inside
    view: Callable[[dict[str, Any]], dict[str, Any]] | None = getattr(fmt, "policy_view", None)
    for rule, reason in request_policy.evaluate(fmt.model(body), view(body) if view else body):
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


PATH_PREFIX = "/_seatbelt/"


class _PathCredentials:
    """`/_seatbelt/{key}/{run}/...`: the run's key and name in the base URL's path rather
    than in headers, for `seatbelt run` recording locally. A CLI sends its custom headers to
    other hosts too (Claude Code sends them to api.anthropic.com's bootstrap endpoint); only
    the requests it points at the base URL carry the path. The prefix is taken off and the two
    become the `x-seatbelt-key` and `x-seatbelt-run` headers, replacing any sent."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] == "http" and path.startswith(PATH_PREFIX):
            key, _, rest = path[len(PATH_PREFIX) :].partition("/")
            run, _, rest = rest.partition("/")
            headers = [
                (k, v)
                for k, v in scope["headers"]
                if k.lower() not in (b"x-seatbelt-key", b"x-seatbelt-run")
            ]
            headers += [(b"x-seatbelt-key", key.encode()), (b"x-seatbelt-run", run.encode())]
            scope = {
                **scope,
                "path": "/" + rest,
                "raw_path": ("/" + rest).encode(),
                "headers": headers,
            }
        await self.app(scope, receive, send)


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
    assemble: Assemble,
    finish: Finish,
    fallback: Callable[[ModelCall, dict[str, Any], str], None],
    settle: Callable[[], Awaitable[None]],
) -> Response:
    """`fallback` records a response as given, for a stream `finish` failed to record."""
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

    def recorded() -> None:
        """In the threadpool: parsing a long stream would hold up every other request. The
        client has the stream already, so one that cannot be recorded as the format's response
        is recorded as the text received, not left an empty response."""
        try:
            finish(call, assemble(sse_events(bytes(received))), outcome[0])
        except Exception as exc:
            if call.response is not None:
                raise
            _log.exception("recording a streamed response failed; recording the stream as sent")
            error = f"stream not recorded: {type(exc).__name__}: {exc}"
            fallback(
                call,
                {
                    "unassembled_sse": bytes(received[:_MAX_UNASSEMBLED]).decode(errors="replace"),
                    "received_bytes": len(received),
                },
                error if outcome[0] is None else f"{outcome[0]}; {error}",
            )

    async def on_close() -> None:
        try:
            await chunks.aclose()
            await resp.aclose()
            await client.aclose()
        finally:
            try:  # never raises by contract; if one does, the session must still be released
                await run_in_threadpool(recorded)
            finally:
                await settle()

    return _RelayStream(chunks, resp.status_code, _relay_headers(resp), on_close)


def create_app(
    cfg: GatewayConfig,
    sessions: Sessions,
    transport: httpx2.AsyncBaseTransport | None = None,
    path_credentials: bool = True,
) -> Starlette:
    """`path_credentials` takes a run's key and name from `/_seatbelt/{key}/{run}/`, for the
    local recorder; `gateway serve` turns it off, as a key in a path ends up in access logs."""

    async def relay(request: Request) -> Response:
        return await record(request, _FORMATS[request.url.path])

    async def gemini_method(request: Request) -> Response:
        """POST /{version}/models/{model}:{method}"""
        model, _, method = str(request.path_params["target"]).rpartition(":")
        if _GEMINI_MODEL.fullmatch(model) and method in _GEMINI_RECORDED:
            return await record(request, _GEMINI, model)
        # a strict model name: nothing Google could read as another method goes unrecorded
        if _GEMINI_MODEL.fullmatch(model) and method in _GEMINI_FORWARDED:
            version = request.url.path.split("/", 2)[1]  # the route's: one of _GEMINI_VERSIONS
            return await forward(request, path=f"/{version}/models/{model}:{method}")
        return _error(request, 400, "invalid_request_error", f"{method or 'this'} is not recorded")

    async def record(request: Request, spec: Spec, path_model: str | None = None) -> Response:
        """Relay an inference request and record it. `path_model` is the model a Gemini
        request names in its URL, put in the body that is recorded (not the one sent)."""
        live = _live(request)
        cfg, request_policy, tool_policy = live.cfg, live.request_policy, live.tool_policy
        principal = await _identify(live, request)
        if isinstance(principal, Response):
            return principal
        upstream_name, auth_header, format_cls, assemble = spec
        upstream_name, prefix = _route(cfg, request, upstream_name)
        upstream = cfg.upstreams.get(upstream_name)
        if upstream is None:
            return _error(request, 404, "not_found_error", f"no {upstream_name} upstream")
        raw = await _body(request)  # sent on as it came, encoding and all
        if raw is None:
            return _too_large(request)
        encoding = request.headers.get("content-encoding", "")
        plain = _decoded(raw, encoding)
        if plain is None:
            return _error(
                request, 415, "invalid_request_error", f"a body encoded {encoding} is not recorded"
            )
        body = _json_object(plain)
        if body is None:
            return _error(request, 400, "invalid_request_error", "body must be a JSON object")
        if _deeper_than(body, _MAX_DEPTH):  # past what the ledger can serialise
            return _error(request, 400, "invalid_request_error", "body is nested too deeply")
        if path_model is not None:
            body = {**body, "model": path_model}
        # a request shape the format cannot record faithfully (Responses `background`, Chat
        # `functions` or n > 1, Gemini snake_case fields or candidateCount > 1) is refused,
        # not forwarded with what the gateway did not read
        unrecordable = format_cls.unrecordable(body)
        if unrecordable is not None:
            return _error(request, 400, "invalid_request_error", unrecordable)
        run = request.headers.get(RUN_HEADER) or None
        run_end = request.headers.get(RUN_END_HEADER, "").lower() == "true"
        meta = {**_meta(request), **principal.attrs}
        session = await run_in_threadpool(
            sessions.get, principal.id, run, meta, principal.session_key
        )
        call: ModelCall | None = None
        handed_off = False  # a stream settles the session itself when it closes
        refused = False  # policy answered the request; no model ever saw it

        def fmt() -> Format:  # under the session lock, so parallel requests share one
            return _format(session, format_cls)

        def finish(call: ModelCall, payload: dict[str, Any] | None, error: str | None) -> None:
            def run() -> None:
                for event in fmt().finish(call, payload, error=error):
                    if tool_policy is not None:
                        _check_tool_call(session, tool_policy, event)

            _locked(session, run)

        def fallback(call: ModelCall, payload: dict[str, Any], error: str) -> None:
            _locked(session, lambda: call.respond(payload, error=error))

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
                await run_in_threadpool(sessions.end, principal.id, run, principal.session_key)
            await run_in_threadpool(sessions.release, session)

        try:
            call, refusal = await run_in_threadpool(_locked, session, admit)
            if refusal is not None:
                refused = True
                return _error(request, 403, "permission_error", refusal)
            client = httpx2.AsyncClient(
                base_url=upstream.url, transport=request.app.state.transport, timeout=600
            )
            outgoing = client.build_request(
                "POST",
                # Claude Code posts /v1/messages?beta=true
                _routed(_with_query(request, upstream, request.url.path), prefix),
                content=raw,
                headers=_upstream_headers(request, upstream, auth_header),
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
                return _error(request, 502, "api_error", error)
            if streaming:
                handed_off = True
                return _stream(resp, client, call, assemble, finish, fallback, settle)
            await client.aclose()
            value = _json(resp.content)
            if isinstance(value, list) and issubclass(format_cls, GeminiFormat):
                # streamGenerateContent without alt=sse answers with a JSON array of chunks
                value = assemble(as_dicts(value))
            payload = cast(dict[str, Any], value) if isinstance(value, dict) else None
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

    async def forward(request: Request, to: str | None = None, path: str | None = None) -> Response:
        """Probes a client makes besides inference: authenticated and forwarded, not recorded.
        A token count or a model list is nothing an auditor needs. `to` names the upstream;
        by default the request's style picks it. `path` is the path to send, built from a
        route's checked parts; by default the route's own, for a route with a fixed path."""
        live = _live(request)
        cfg = live.cfg
        principal = await _identify(live, request)
        if isinstance(principal, Response):
            return principal
        # Anthropic clients send anthropic-version on every request, x-api-key or Bearer alike
        # (Claude Code under `seatbelt run`, Claude Desktop by default); OpenAI clients never do
        anthropic_style = (
            request.url.path.startswith("/v1/messages")
            or "anthropic-version" in request.headers
            or "x-api-key" in request.headers
        )
        if to is not None:
            name, auth_header = to, "authorization"
        elif _google_style(request):
            name, auth_header = "gemini", "x-goog-api-key"
        elif anthropic_style:
            name, auth_header = "anthropic", "x-api-key"
        else:
            name, auth_header = "openai", "authorization"
        name, prefix = _route(cfg, request, name)
        upstream = cfg.upstreams.get(name)
        if upstream is None:
            return _error(request, 404, "not_found_error", f"no {name} upstream")
        target = _routed(_with_query(request, upstream, path or request.url.path), prefix)
        content: bytes | None = None  # a GET or HEAD carries none upstream, whatever was sent
        if request.method not in ("GET", "HEAD"):
            content = await _body(request)
            if content is None:
                return _too_large(request)
        async with httpx2.AsyncClient(
            base_url=upstream.url, transport=request.app.state.transport, timeout=60
        ) as client:
            try:
                resp = await client.request(
                    request.method,
                    target,
                    content=content,
                    headers=_upstream_headers(request, upstream, auth_header),
                )
            except httpx2.HTTPError as exc:
                return _error(
                    request, 502, "api_error", f"upstream unreachable: {type(exc).__name__}: {exc}"
                )
        return Response(resp.content, status_code=resp.status_code, headers=_relay_headers(resp))

    async def code_assist(request: Request) -> Response:
        """/v1internal:{method}: the two inference methods recorded, the account and quota
        calls Gemini CLI makes forwarded, any other refused rather than let through
        unrecorded."""
        method = str(request.path_params["method"])
        if method in _GEMINI_RECORDED and request.method == "POST":
            return await record(request, _CODE_ASSIST)
        if method in _CODE_ASSIST_FORWARDED:
            return await forward(request, "codeassist", f"/v1internal:{method}")
        return _error(request, 400, "invalid_request_error", f"{method} is not recorded")

    async def code_assist_operation(request: Request) -> Response:
        """GET /v1internal/operations/{id}: onboarding. Nothing else under /v1internal/."""
        name = str(request.path_params["name"])
        if not _CODE_ASSIST_OPERATION.fullmatch(name):
            return _error(request, 400, "invalid_request_error", "not an operation")
        return await forward(request, "codeassist", f"/v1internal/{name}")

    async def model_lookup(request: Request) -> Response:
        """GET /{version}/models/{id}: one model. The id is checked and the path sent built
        from it, so no other resource of an upstream is reached with the gateway's key."""
        model = str(request.path_params["id"])
        pattern = _GEMINI_MODEL if _google_style(request) else _MODEL_ID
        if not pattern.fullmatch(model):
            return _error(request, 400, "invalid_request_error", "not a model id")
        version = request.url.path.split("/", 2)[1]  # the route's
        return await forward(request, path=f"/{version}/models/{model}")

    async def hello(_: Request) -> Response:
        return Response(status_code=200)

    async def end_run(request: Request) -> Response:
        principal = await _identify(_live(request), request)
        if isinstance(principal, Response):
            return principal
        name = request.path_params["name"]
        found = await run_in_threadpool(sessions.end, principal.id, name, principal.session_key)
        return Response(status_code=204 if found else 404)

    app = Starlette(
        routes=[
            Route("/v1/messages", relay, methods=["POST"]),
            Route("/v1/chat/completions", relay, methods=["POST"]),
            Route("/v1/responses", relay, methods=["POST"]),
            Route("/v1/messages/count_tokens", forward, methods=["POST"]),
            Route("/v1/models", forward, methods=["GET"]),
            Route("/v1/models/{id:path}", model_lookup, methods=["GET"]),
            *(
                Route(f"/{v}/models/{{target:path}}", gemini_method, methods=["POST"])
                for v in _GEMINI_VERSIONS
            ),
            Route("/v1beta/models", forward, methods=["GET"]),
            Route("/v1beta/models/{id:path}", model_lookup, methods=["GET"]),
            Route("/v1alpha/models", forward, methods=["GET"]),
            Route("/v1alpha/models/{id:path}", model_lookup, methods=["GET"]),
            Route("/v1internal:{method}", code_assist, methods=["GET", "POST"]),
            Route("/v1internal/{name:path}", code_assist_operation, methods=["GET"]),
            Route("/api/hello", hello, methods=["GET", "HEAD"]),
            Route("/seatbelt/runs/{name}/end", end_run, methods=["POST"]),
        ]
    )
    if path_credentials:
        app.add_middleware(_PathCredentials)
    app.state.live = Live.of(cfg)  # replaced by a config reload
    app.state.transport = transport  # read per request so tests can swap upstreams
    return app
