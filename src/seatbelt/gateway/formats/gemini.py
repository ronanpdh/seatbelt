"""Gemini API `generateContent` wire format on plain JSON: request/response dicts and SSE events.

The model is in the URL (`/v1beta/models/{model}:generateContent`), not the body: the gateway
puts it in the body under `model` before this module sees it. The wire facts and their
sources: docs/plans/2026-09-28-gemini-format.md. Every ``Any`` here is provider JSON: untyped
because the wire shape is not ours to pin.
"""

from __future__ import annotations

import json
from typing import Any

from seatbelt.gateway.formats import as_dict, as_dicts
from seatbelt.ledger.events import Event
from seatbelt.record.recorder import ModelCall, Recorder

KEEP = (
    "contents",
    "systemInstruction",
    "tools",
    "toolConfig",
    "generationConfig",
    "safetySettings",
    "cachedContent",  # a stored context that comes before the contents
    "labels",
    "serviceTier",
    "store",
)
# usageMetadata -> usage, named as the other formats name them. Gemini counts thinking apart
# from the answer (total = prompt + thoughts + candidates); OpenAI's output_tokens includes its
# reasoning tokens, so output_tokens here is candidates + thoughts, with the thoughts beside
_USAGE = {
    "promptTokenCount": "input_tokens",
    "cachedContentTokenCount": "input_tokens.cached_tokens",  # part of the prompt count
    "thoughtsTokenCount": "output_tokens.reasoning_tokens",
    "toolUsePromptTokenCount": "tool_use_prompt_tokens",
    "totalTokenCount": "total_tokens",
}


def _parts(content: Any) -> list[dict[str, Any]]:
    return as_dicts(as_dict(content).get("parts"))


def _responses(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Every functionResponse in the request's contents, in order."""
    return [
        fr
        for content in as_dicts(body.get("contents"))
        for part in _parts(content)
        if (fr := as_dict(part.get("functionResponse")))
    ]


def _result_key(fr: dict[str, Any], seen: dict[str, int]) -> str:
    """What identifies a result across resends of the history: its id, or with none its
    name, content and how many identical ones came before it in this request."""
    if fr.get("id"):
        return f"id\0{fr['id']}"
    content = json.dumps([fr.get("name"), fr.get("response")], sort_keys=True, default=str)
    seen[content] = seen.get(content, 0) + 1
    return f"content\0{content}\0{seen[content]}"


class GeminiFormat:
    provider = "gemini"

    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self._open: dict[str, Event] = {}  # call id -> tool.call event, oldest first
        self._answered: set[str] = set()  # results already recorded, by `_result_key`
        self._unmatched: set[str] = set()  # results seen that answered no open call
        self._minted = 0

    @staticmethod
    def model(body: dict[str, Any]) -> str:
        return str(body.get("model") or "unknown")

    def _match(
        self, fr: dict[str, Any], history: dict[str, dict[str, Any]], loose: bool = True
    ) -> Event | None:
        """The open call a result answers: by id, or by the id less a `<name>__` prefix
        (Gemini CLI prefixes ids internally and strips the prefix before it sends; a request
        that skipped that would carry it). Failing that (the API gave the call no id and the
        client made one up, as Gemini CLI does), the oldest open call to the same tool, with
        the arguments of the request's own call of that id when it has one, so an old result
        resent from before this session does not claim a new call. `history` is the
        arguments of the request's function calls, by id."""
        name = str(fr.get("name") or "")
        rid = str(fr.get("id") or "")
        for key in (rid, rid.removeprefix(f"{name}__") if name else ""):
            if key and key in self._open:
                return self._open.pop(key)
        if not loose:
            return None
        args = history.get(rid) if rid else None
        for key, call in self._open.items():
            if call.attrs.get("gen_ai.tool.name") != name:
                continue
            if args is None or call.attrs.get("gen_ai.tool.call.arguments") == args:
                return self._open.pop(key)
        return None

    def begin(self, body: dict[str, Any]) -> ModelCall:
        """Record tool results carried in the contents, then the request. Clients resend the
        whole history each turn; each result is recorded once."""
        seen: dict[str, int] = {}
        history = {
            str(fc["id"]): as_dict(fc.get("args"))
            for content in as_dicts(body.get("contents"))
            for part in _parts(content)
            if (fc := as_dict(part.get("functionCall"))).get("id")
        }
        for fr in _responses(body):
            key = _result_key(fr, seen)
            if key in self._answered:
                continue
            # a result that answered no call when first seen is an old one (a history resent
            # from before this session): only its exact id may match later, never a new
            # call to the same tool by name
            call = self._match(fr, history, loose=key not in self._unmatched)
            if call is None:
                self._unmatched.add(key)
                continue
            self._answered.add(key)
            response = as_dict(fr.get("response"))
            # Gemini CLI reports a failed tool as {"error": ...}, a result as {"output": ...}
            failed = "error" in response and "output" not in response
            self._rec.tool_returned(
                call, fr.get("response"), str(response["error"]) if failed else None
            )
        request = {k: body[k] for k in KEEP if k in body}
        return self._rec.model_requested(self.model(body), request, provider=self.provider)

    @staticmethod
    def tool_result_calls(body: dict[str, Any]) -> list[tuple[str, str | None]]:
        """(call id, tool name) for every functionResponse in the request. The name is
        required on the wire, so a denied tool's result is refused by its name even when the
        id is one the client made up."""
        return [
            (str(fr.get("id") or fr.get("name")), str(fr["name"]) if fr.get("name") else None)
            for fr in _responses(body)
        ]

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]:
        """Record the response and any function calls it asks for; returns the tool.call
        events. A functionCall part arrives whole (the Gemini API does not stream partial
        arguments), so a call is recorded even from a stream that broke after it."""
        if response is None:
            call.respond({}, error=error)
            return []
        candidates = as_dicts(response.get("candidates"))
        block = as_dict(response.get("promptFeedback")).get("blockReason")
        if error is None and not candidates and block:
            error = f"prompt blocked: {block}"
        model = response.get("modelVersion")
        answer = call.respond(
            response,
            _usage(as_dict(response.get("usageMetadata"))),
            response_model=model if isinstance(model, str) else None,
            error=error,
        )
        if not candidates:
            return []
        calls: list[Event] = []
        for part in _parts(candidates[0].get("content")):
            fc = as_dict(part.get("functionCall"))
            if not fc.get("name"):
                continue
            call_id = str(fc.get("id") or "")
            if not call_id:
                self._minted += 1
                call_id = f"{fc['name']}#{self._minted}"  # the API gave none
            event = self._rec.tool_called(
                str(fc["name"]), as_dict(fc.get("args")), call_id=call_id, parent_id=answer.id
            )
            self._open[call_id] = event
            calls.append(event)
        return calls


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    out = {_USAGE[k]: v for k, v in raw.items() if k in _USAGE and isinstance(v, int)}
    answer = raw.get("candidatesTokenCount")
    thoughts = raw.get("thoughtsTokenCount")
    if isinstance(answer, int) or isinstance(thoughts, int):
        out["output_tokens"] = sum(n for n in (answer, thoughts) if isinstance(n, int))
    return out


def _mergeable(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two adjacent text parts of the same kind (answer or thought) join into one."""
    return (
        isinstance(a.get("text"), str)
        and isinstance(b.get("text"), str)
        and bool(a.get("thought")) == bool(b.get("thought"))
    )


def assemble_sse(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild the response from the stream's chunks, as far as they got. Each chunk is a
    whole GenerateContentResponse carrying the parts produced since the last: text parts are
    joined, others (function calls and the like) kept in order, and the last value of every
    other field wins, as Gemini CLI reads a stream. Never raises: the gateway calls this in a
    `finally`, and a malformed stream must still be recorded."""
    out: dict[str, Any] = {}
    candidates: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        out.update({k: v for k, v in chunk.items() if k != "candidates" and v})
        for n, cand in enumerate(as_dicts(chunk.get("candidates"))):
            i = cand.get("index", n)
            if not isinstance(i, int):
                continue
            slot = candidates.setdefault(i, {"index": i, "content": {"parts": []}})
            content = as_dict(cand.get("content"))
            parts: list[dict[str, Any]] = slot["content"]["parts"]
            for part in _parts(content):
                if parts and _mergeable(parts[-1], part):
                    parts[-1] = {**parts[-1], **part, "text": parts[-1]["text"] + part["text"]}
                else:
                    parts.append(dict(part))
            if isinstance(content.get("role"), str):
                slot["content"]["role"] = content["role"]
            slot.update({k: v for k, v in cand.items() if k not in ("content", "index") and v})
    if candidates:
        out["candidates"] = [candidates[i] for i in sorted(candidates)]
    return out


class CodeAssistFormat(GeminiFormat):
    """Gemini CLI signed in with Google: the same content, wrapped for Google's Code Assist
    service (`POST /v1internal:generateContent`), as `{model, project, user_prompt_id,
    request: <GenerateContentRequest>}` in and `{response: <GenerateContentResponse>,
    traceId}` out (gemini-cli packages/core/src/code_assist/converter.ts)."""

    @staticmethod
    def inner(body: dict[str, Any]) -> dict[str, Any]:
        """The GenerateContentRequest inside, with the model as the gateway records it."""
        return {**as_dict(body.get("request")), "model": body.get("model")}

    policy_view = inner  # the request policy reads, as for any Gemini request

    def begin(self, body: dict[str, Any]) -> ModelCall:
        return super().begin(self.inner(body))

    @staticmethod
    def tool_result_calls(body: dict[str, Any]) -> list[tuple[str, str | None]]:
        return GeminiFormat.tool_result_calls(CodeAssistFormat.inner(body))

    def finish(
        self, call: ModelCall, response: dict[str, Any] | None, error: str | None = None
    ) -> list[Event]:
        return super().finish(call, None if response is None else unwrap(response), error)


def unwrap(response: dict[str, Any]) -> dict[str, Any]:
    """A Code Assist response as the GenerateContentResponse it carries, with its traceId."""
    trace = response.get("traceId")
    return {**as_dict(response.get("response")), **({"traceId": trace} if trace else {})}


def assemble_code_assist_sse(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """`assemble_sse` over the wrapped chunks; returns the wrapped form `finish` unwraps."""
    whole = assemble_sse([unwrap(c) for c in chunks])
    trace = whole.pop("traceId", None)
    return {"response": whole, **({"traceId": trace} if trace else {})}
