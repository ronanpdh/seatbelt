"""Compliance API transcripts -> ledger events.

The three transcript shapes (local sessions, remote sessions, chats) share `text`, `tool_use`
and `tool_result` blocks; they differ in how a message says where it came from. Each message
becomes events of the kinds the gateway writes, each carrying the API's own id and timestamp.
No `model.request` is written: the API returns the conversation, not the requests behind it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from seatbelt.gateway.formats import as_dict, as_dicts
from seatbelt.ledger.events import Event
from seatbelt.record.recorder import Recorder

RETENTION_ELAPSED = "retention_elapsed"


@dataclass
class Message:
    """One transcript message, in the form every source is read into."""

    id: str
    role: str  # "user" or "assistant"
    created_at: str | None
    content: list[dict[str, Any]]
    model: str | None = None  # the model that served it, when the source says
    provenance: dict[str, Any] | None = None  # None: content the API captured and verifies
    sender: str | None = None  # who sent a user message, when not the conversation's owner
    extra: dict[str, Any] = field(default_factory=dict[str, Any])  # recorded as is


def local_message(raw: dict[str, Any]) -> Message:
    prov = raw.get("provenance")
    return Message(
        id=str(raw.get("id")),
        role=str(raw.get("role")),
        created_at=raw.get("created_at"),
        content=as_dicts(raw.get("content")),
        model=raw.get("model") if isinstance(raw.get("model"), str) else None,
        provenance=as_dict(prov) if isinstance(prov, dict) else None,
    )


def remote_message(raw: dict[str, Any]) -> Message:
    sender = raw.get("sent_by_user_id")
    return Message(
        id=str(raw.get("id")),
        role=str(raw.get("role")),
        created_at=raw.get("created_at"),
        content=as_dicts(raw.get("content")),
        # remote messages say only that the content could not be returned, never why
        provenance={"type": "content_unavailable"} if raw.get("content_unavailable") else None,
        sender=sender if isinstance(sender, str) else None,
    )


def chat_message(raw: dict[str, Any]) -> Message:
    extra = {
        f"compliance.{k}": raw[k] for k in ("files", "generated_files", "artifacts") if raw.get(k)
    }
    return Message(
        id=str(raw.get("id")),
        role=str(raw.get("role")),
        created_at=raw.get("created_at"),
        content=as_dicts(raw.get("content")),
        extra=extra,
    )


def is_retention_placeholder(msg: Message) -> bool:
    p = msg.provenance or {}
    return p.get("type") == "content_unavailable" and p.get("reason") == RETENTION_ELAPSED


def _texts(blocks: list[dict[str, Any]]) -> list[str]:
    return [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]


def _flags(block: dict[str, Any], *names: str) -> dict[str, Any]:
    return {f"compliance.{n}": block[n] for n in names if block.get(n)}


class Writer:
    """Writes one segment's messages. Tool calls are matched to their results within it."""

    def __init__(self, rec: Recorder, owner: str, chat_model: str | None = None) -> None:
        self._rec = rec
        self._owner = owner
        self._chat_model = chat_model
        self._calls: dict[str, Event] = {}  # call key -> tool.call, unanswered, oldest first
        self.last_message: str | None = None
        self.messages = 0

    def write(self, msg: Message) -> None:
        attrs: dict[str, Any] = {
            "compliance.message_id": msg.id,
            "compliance.created_at": msg.created_at,
            **({"compliance.provenance": msg.provenance} if msg.provenance else {}),
            **msg.extra,
        }
        if msg.role == "assistant":
            self._assistant(msg, attrs)
        else:
            self._user(msg, attrs)
        self.last_message = msg.id
        self.messages += 1

    def _user(self, msg: Message, attrs: dict[str, Any]) -> None:
        texts = _texts(msg.content)
        unknown = [b for b in msg.content if b.get("type") not in ("text", "tool_result")]
        results = [b for b in msg.content if b.get("type") == "tool_result"]
        if texts or unknown or not results:  # an unavailable message still gets its event
            content = "\n".join(texts + [json.dumps(b, sort_keys=True) for b in unknown])
            self._rec.user_message(msg.sender or self._owner, content, attrs)
        for block in results:
            self._result(block, attrs)

    def _assistant(self, msg: Message, attrs: dict[str, Any]) -> None:
        text = [
            {"type": "text", "text": b.get("text", ""), **_flags(b, "truncated")}
            for b in msg.content
            if b.get("type") == "text"
        ]
        unknown = [b for b in msg.content if b.get("type") not in ("text", "tool_use")]
        extra = dict(attrs)
        if self._chat_model is not None:
            extra["compliance.chat_model"] = self._chat_model  # selected, not necessarily served
        if any(b.get("thinking_redacted") for b in msg.content):
            extra["compliance.thinking_redacted"] = True
        answer = self._rec.model_responded(
            msg.model, {"role": "assistant", "content": text + unknown}, extra
        )
        for n, block in enumerate(msg.content):
            if block.get("type") == "tool_use":
                self._call(block, f"{msg.id}#{n}", answer, attrs)

    def _call(
        self, block: dict[str, Any], fallback: str, answer: Event, attrs: dict[str, Any]
    ) -> None:
        key = str(block.get("id") or fallback)
        raw = block.get("input")
        args: dict[str, Any] = {}
        extra = {**attrs, **_flags(block, "truncated", "integration_name", "mcp_server_url")}
        parsed: Any = None
        if isinstance(raw, str) and not block.get("truncated"):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            args = parsed  # pyright: ignore[reportUnknownVariableType] - tool JSON
        elif raw is not None:
            extra["compliance.input"] = raw  # truncated or not a JSON object: kept as sent
        call = self._rec.tool_called(
            str(block.get("name")), args, call_id=key, attrs=extra, parent_id=answer.id
        )
        self._calls[key] = call

    def _result(self, block: dict[str, Any], attrs: dict[str, Any]) -> None:
        use_id = block.get("tool_use_id")
        name = str(block.get("name"))
        call = self._calls.pop(str(use_id), None) if use_id else None
        if call is None and not use_id:  # no id: the oldest unanswered call to that tool
            key = next(
                (k for k, c in self._calls.items() if c.attrs.get("gen_ai.tool.name") == name),
                None,
            )
            call = self._calls.pop(key) if key is not None else None
        texts = _texts(as_dicts(block.get("content")))
        error = "\n".join(texts) if block.get("is_error") else None
        extra = {
            **attrs,
            "gen_ai.tool.call.id": use_id,
            **_flags(block, "truncated", "integration_name", "mcp_server_url"),
        }
        self._rec.tool_returned(call, None if error else texts, error, extra, name=name)
