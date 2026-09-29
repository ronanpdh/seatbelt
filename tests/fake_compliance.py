"""A fake of Anthropic's Compliance API, serving the shapes its docs give
(platform.claude.com/docs/en/manage-claude/compliance-sessions and compliance-content-data):
session lists paged with `page`/`next_page`, the chat list paged with `after_id`/`last_id`/
`has_more`, and pages kept small so every walk crosses a page boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2

KEY = "sk-ant-api01-test"
BASE = "/v1/compliance/apps"


def at(minute: int) -> str:
    """A fixed day's timestamp, `minute` minutes past midnight UTC."""
    return f"2026-09-01T{minute // 60:02d}:{minute % 60:02d}:00Z"


@dataclass
class FakeCompliance:
    page_size: int = 2
    local: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    remote: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    chats: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])
    # answered before the normal handling, once each, when the predicate matches
    faults: list[tuple[Callable[[httpx2.Request], bool], httpx2.Response]] = field(
        default_factory=list[tuple[Callable[[httpx2.Request], bool], httpx2.Response]]
    )
    served: int = 0

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.headers.get("x-api-key") != KEY:
            return _error(401, "authentication_error", "invalid x-api-key")
        if request.headers.get("anthropic-version") != "2023-06-01":
            return _error(400, "invalid_request_error", "anthropic-version required")
        for n, (matches, response) in enumerate(self.faults):
            if matches(request):
                del self.faults[n]
                return response
        self.served += 1
        resp = self._route(request)
        resp.headers["request-id"] = f"req_{self.served}"
        return resp

    def _route(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix(BASE)
        q = request.url.params
        parts = [p for p in path.split("/") if p]
        if parts[:2] == ["sessions", "local"]:
            return self._sessions(self.local, parts[2:], q, "compliance_local_session")
        if parts[:2] == ["sessions", "remote"]:
            return self._sessions(self.remote, parts[2:], q, None)
        if parts[:1] == ["chats"]:
            return self._chats(parts[1:], q)
        return _error(404, "not_found_error", "no such endpoint")

    def _sessions(
        self, store: dict[str, dict[str, Any]], rest: list[str], q: Any, kind: str | None
    ) -> httpx2.Response:
        if not rest:
            rows = [s["meta"] for s in store.values()]
            if "updated_at.gte" in q:
                rows = [r for r in rows if r["updated_at"] >= q["updated_at.gte"]]
            if "created_at.gte" in q:
                rows = [r for r in rows if r["created_at"] >= q["created_at.gte"]]
            rows.sort(key=lambda r: r["created_at"], reverse=True)  # newest first
            return self._paged(rows, q)
        session = store.get(rest[0])
        if session is None:
            return _error(404, "not_found_error", "session not found")
        if len(rest) == 1 and kind is not None:
            return httpx2.Response(200, json=session["meta"])
        if session["meta"].get("status") == "pending":
            return _error(404, "not_found_error", "session is pending")
        meta = {**session["meta"]}
        if kind is None:  # the remote envelope never carries these
            meta.update(claude_project_id=None, started_by_user=None)
        if meta.get("user"):
            meta["user"] = {**meta["user"], "email_address": None}
        page = self._paged(session["messages"], q).json()
        return httpx2.Response(200, json={"session": meta, **page})

    def _paged(self, rows: list[dict[str, Any]], q: Any) -> httpx2.Response:
        start = int(q.get("page", "0"))
        size = min(int(q.get("limit", "100")), self.page_size)
        chunk = rows[start : start + size]
        more = start + size < len(rows)
        return httpx2.Response(
            200, json={"data": chunk, "next_page": str(start + size) if more else None}
        )

    def _chats(self, rest: list[str], q: Any) -> httpx2.Response:
        if not rest:
            rows = sorted((c["meta"] for c in self.chats.values()), key=_chat_cursor)
            if "updated_at.gte" in q:
                rows = [r for r in rows if r["updated_at"] >= q["updated_at.gte"]]
            if "after_id" in q:
                rows = [r for r in rows if _chat_cursor(r) > tuple(q["after_id"].split("|"))]
            size = min(int(q.get("limit", "100")), self.page_size)
            chunk = rows[:size]
            last = "|".join(_chat_cursor(chunk[-1])) if chunk else None
            return httpx2.Response(
                200,
                json={
                    "data": chunk,
                    "has_more": len(rows) > size,
                    "first_id": "|".join(_chat_cursor(chunk[0])) if chunk else None,
                    "last_id": last,
                },
            )
        chat = self.chats.get(rest[0])
        if chat is None or rest[1:] != ["messages"]:
            return _error(404, "not_found_error", "chat not found")
        messages = chat["messages"]
        if chat["meta"].get("deleted_at"):
            messages = [{**m, "content": []} for m in messages]
        return httpx2.Response(
            200, json={**chat["meta"], "chat_messages": messages, "has_more": False}
        )


def _chat_cursor(row: dict[str, Any]) -> tuple[str, str]:
    return (row["updated_at"], row["id"])


def _error(status: int, kind: str, message: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": kind, "message": message}}
    )


def error(status: int, message: str, **headers: str) -> httpx2.Response:
    kind = {429: "rate_limit_error", 503: "overloaded_error"}.get(status, "api_error")
    resp = _error(status, kind, message)
    resp.headers.update(headers)
    return resp
