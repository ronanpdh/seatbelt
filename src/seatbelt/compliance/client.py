"""A read-only client for Anthropic's Compliance API, with the retry contract its docs give
(platform.claude.com/docs/en/manage-claude/compliance-errors): honour `retry-after` on 429,
never retry a 500 marked `x-should-retry: false`, back off from 1 s to 60 s on other 5xx, and
tell apart the local-session 503s that must not be waited out."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx2

_log = logging.getLogger(__name__)

VERSION = "2023-06-01"
MAX_ATTEMPTS = 7  # 1 + 2 + 4 + ... + 32 s: about a minute of backoff before giving up
RESERVE = 30  # requests left in the shared window below which the client waits for the reset
_TRANSIENT = {500, 502, 503, 504, 529}


class ComplianceError(Exception):
    """A request that failed for good. Never carries the key."""

    def __init__(self, status: int | None, message: str, request_id: str | None = None) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.request_id = request_id


class NotFound(ComplianceError):
    """404: gone, never there, not readable with this key, or (remote) still pending."""


class TryLater(ComplianceError):
    """A local-session 503 "Try again later.": skip it and come back on a later run."""


class ContentUnavailable(ComplianceError):
    """A local-session "Captured content" 503 that kept recurring: the org's customer-managed
    key may be unusable."""


def backoff(attempt: int) -> float:
    return float(min(60, 2**attempt))


def _message(resp: httpx2.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return str(err.get("message") or resp.text[:300])
    except (ValueError, AttributeError):
        return resp.text[:300]


class ComplianceClient:
    def __init__(
        self,
        key: str,
        url: str = "https://api.anthropic.com",
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = httpx2.Client(
            base_url=url.rstrip("/"),
            transport=transport,
            timeout=120,
            headers={"x-api-key": key, "anthropic-version": VERSION},
        )
        self._sleep = sleep
        self._now = now
        self.request_ids: list[str] = []  # every successful response's, for chain of custody

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a JSON object, retrying what the docs say to retry."""
        remote = "/sessions/remote" in path
        attempt = 0
        while True:
            try:
                resp = self._client.get(path, params=params)
            except httpx2.HTTPError as exc:
                if attempt + 1 >= MAX_ATTEMPTS:
                    raise ComplianceError(None, f"{type(exc).__name__}: {exc}") from exc
                self._wait(backoff(attempt), f"{type(exc).__name__}")
                attempt += 1
                continue
            rid = resp.headers.get("request-id")
            status = resp.status_code
            if status < 300:
                self._throttle(resp)
                if rid:
                    self.request_ids.append(rid)
                data: Any = resp.json()
                if not isinstance(data, dict):
                    raise ComplianceError(status, "expected a JSON object", rid)
                return data  # pyright: ignore[reportUnknownVariableType] - provider JSON
            message = _message(resp)
            if status == 404:
                raise NotFound(status, message, rid)
            if status == 503 and "Try again later" in message:
                raise TryLater(status, message, rid)
            retry = status == 429 or (
                status in _TRANSIENT and resp.headers.get("x-should-retry") != "false"
            )
            if not retry or attempt + 1 >= MAX_ATTEMPTS:
                if status == 503 and message.startswith("Captured content"):
                    raise ContentUnavailable(status, message, rid)
                raise ComplianceError(status, message, rid)
            delay = backoff(attempt)
            if status == 429:
                after = resp.headers.get("retry-after")
                if after is not None and after.isdigit():
                    # the remote budget's retry-after is always 1: back off when it repeats
                    delay = max(float(after), delay if remote and attempt else 0.0)
            self._wait(delay, str(status))
            attempt += 1

    def _wait(self, delay: float, why: str) -> None:
        _log.info("compliance API: %s, retrying in %.0fs", why, delay)
        self._sleep(delay)

    def _throttle(self, resp: httpx2.Response) -> None:
        """Slow down before the shared limit runs out, so other tools on the org keep some."""
        left = resp.headers.get("anthropic-ratelimit-requests-remaining")
        reset = resp.headers.get("anthropic-ratelimit-requests-reset")
        if left is None or not left.isdigit() or int(left) > RESERVE or reset is None:
            return
        try:
            wait = (datetime.fromisoformat(reset) - self._now()).total_seconds()
        except ValueError:
            return
        if wait > 0:
            self._wait(min(wait, 60.0), f"{left} requests left in this minute")

    def pages(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Each page of a list paged with `page` and `next_page` (the session endpoints)."""
        query = dict(params)
        while True:
            page = self.get(path, query)
            yield page
            token = page.get("next_page")
            if not token:
                return
            query["page"] = token
