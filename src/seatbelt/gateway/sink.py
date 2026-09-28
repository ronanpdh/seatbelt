"""Ship each closed ledger and its signature to S3-compatible object storage.

The gateway host then no longer holds the only copy of the evidence. Uploads run on one
background thread, so a slow or unreachable store never delays a request; a failed upload is
retried with backoff, and anything not shipped by shutdown is shipped at the next start.

Requests are signed with AWS Signature Version 4 over the whole payload, with no checksum
headers, and address the bucket in the host name, as Hetzner Object Storage documents for
plain HTTP clients (docs.hetzner.com/storage/object-storage/getting-started/using-curl)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx2

from seatbelt.attest.manifest import sidecar
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import LedgerError, read_events

_log = logging.getLogger(__name__)

BACKOFF = (5.0, 30.0, 120.0, 300.0)  # seconds between attempts; the last repeats


class StoreError(Exception):
    pass


class Store(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(
    method: str,
    url: str,
    payload: bytes,
    headers: dict[str, str],
    region: str,
    access_key: str,
    secret_key: str,
    now: datetime,
    service: str = "s3",
) -> dict[str, str]:
    """`headers` plus `x-amz-date`, `x-amz-content-sha256` and `Authorization`, signing
    every header given and the host. The URL must have no query string."""
    parts = urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    payload_hash = hashlib.sha256(payload).hexdigest()
    signed = {k.lower(): " ".join(v.split()) for k, v in headers.items()}
    signed |= {"host": parts.netloc, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    names = sorted(signed)
    canonical = "\n".join(
        [
            method,
            parts.path or "/",
            "",  # no query string
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date), region), service), "aws4_request"
    )
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        **headers,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}"
        ),
    }


class S3Store:
    """PUT objects into one bucket of an S3-compatible service, e.g. Hetzner Object Storage:
    endpoint `https://fsn1.your-objectstorage.com`, region `fsn1`."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        parts = urlsplit(endpoint)
        self._base = f"{parts.scheme}://{bucket}.{parts.netloc}"  # the bucket in the host
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._clock = clock
        self._client = httpx2.Client(transport=transport, timeout=60)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        url = f"{self._base}/{quote(key, safe='/-_.~')}"
        headers = sigv4_headers(
            "PUT",
            url,
            data,
            {"content-type": content_type},
            self._region,
            self._access_key,
            self._secret_key,
            self._clock(),
        )
        try:
            resp = self._client.put(url, content=data, headers=headers)
        except httpx2.HTTPError as exc:
            raise StoreError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 300:
            raise StoreError(f"{resp.status_code}: {resp.text[:300]}")

    def close(self) -> None:
        self._client.close()


def _closed(path: Path) -> bool:
    try:
        events = list(read_events(path))
    except (OSError, UnicodeDecodeError, LedgerError):
        return False
    return bool(events) and events[-1].kind is Kind.RUN_END


class Sink:
    """Ships closed ledgers, each with its signature, once. What has shipped is recorded in
    `<ledgers>/.shipped/<run id>` (the object keys and the SHA-256 of what was sent)."""

    def __init__(
        self,
        store: Store,
        root: Path,
        prefix: str = "",
        backoff: tuple[float, ...] = BACKOFF,
    ) -> None:
        self._store = store
        self._root = root
        self._prefix = prefix
        self._backoff = backoff
        self._marks = root / ".shipped"
        self._marks.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: Path | None = None  # being uploaded

    def shipped(self, path: Path) -> bool:
        return (self._marks / path.stem).exists()

    def ship(self, path: Path) -> None:
        """Queue a closed ledger. Returns at once."""
        self._queue.put(path)

    def catch_up(self) -> int:
        """Queue every closed ledger not shipped yet: after a restart, or a store that was down.
        Returns how many."""
        pending = [
            p for p in sorted(self._root.glob("*.jsonl")) if not self.shipped(p) and _closed(p)
        ]
        for p in pending:
            self.ship(p)
        return len(pending)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="seatbelt-sink", daemon=True)
        self._thread.start()

    def stop(self, timeout: float) -> int:
        """Ship what is queued for up to `timeout` seconds; returns how many are left, which
        the next start ships."""
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout)
        self._stop.set()  # a retry still waiting gives up; its ledger stays for catch_up
        if self._thread is not None:
            self._thread.join(5)
        left = 0
        while not self._queue.empty():
            left += self._queue.get_nowait() is not None
        return left + (self._current is not None)

    def _run(self) -> None:
        while (path := self._queue.get()) is not None:
            self._current = path
            attempt = 0
            while True:
                try:
                    self._upload(path)
                    break
                except Exception as exc:  # the store's failures, and any bug, are retried
                    delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                    _log.warning("could not ship %s (retry in %.0fs): %s", path.name, delay, exc)
                    attempt += 1
                    if self._stop.wait(delay):
                        return  # stopping: this ledger and the queue are left for catch_up
            self._current = None

    def _upload(self, path: Path) -> None:
        if self.shipped(path):
            return
        sent: dict[str, str] = {}
        for file, content_type in (
            (path, "application/x-ndjson"),
            (sidecar(path), "application/json"),
        ):
            if not file.exists():
                continue  # a gateway without a signing key writes no sidecar
            data = file.read_bytes()
            key = self._prefix + file.name
            self._store.put(key, data, content_type)
            sent[key] = hashlib.sha256(data).hexdigest()
        (self._marks / path.stem).write_text(json.dumps(sent, indent=1) + "\n")
        _log.info("shipped %s", path.name)
