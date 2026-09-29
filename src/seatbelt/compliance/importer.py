"""`seatbelt import compliance`: one pass over the Compliance API.

Each conversation (a local session, a remote session or a chat) is imported once it has been
quiet for `settle`, as a signed ledger of the messages not recorded before. A conversation that
grows later gets another ledger, a segment, chained to the last by its run id and hash. Run ids
are deterministic, so a killed run or a lost state file never records a message twice.
Design and sources: docs/plans/2026-09-29-compliance-importer-design.md.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer
from seatbelt.compliance.client import (
    ComplianceClient,
    ComplianceError,
    ContentUnavailable,
    NotFound,
    TryLater,
)
from seatbelt.compliance.mapping import (
    Message,
    Writer,
    chat_message,
    is_retention_placeholder,
    local_message,
    remote_message,
)
from seatbelt.erase import id_hash, read_erased
from seatbelt.gateway.config import ComplianceConfig
from seatbelt.gateway.formats import as_dict, as_dicts
from seatbelt.gateway.sessions import close_open_chains
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import LedgerError, read_events
from seatbelt.locks import try_lock
from seatbelt.record.recorder import Recorder

_log = logging.getLogger(__name__)

LOCAL = "/v1/compliance/apps/sessions/local"
REMOTE = "/v1/compliance/apps/sessions/remote"
CHATS = "/v1/compliance/apps/chats"
FULL = -1  # the server's maximum for tool inputs and results, instead of the 10,000 default
FINAL = {"archived", "failed"}  # remote statuses after which a session is not followed (ours)
FOLLOW = timedelta(days=30)  # how long a quiet, unfinished remote session is still followed
KILLED = "importer killed"
STUCK_AFTER = 3  # runs in a row a pending session cannot be read before a person must look
STATE = ".state.json"
LOCK = ".lock"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe(value: str) -> str:
    return _UNSAFE.sub(".", value)


@dataclass
class Conversation:
    """What the importer knows about one conversation, kept in the state file."""

    source: str  # local_session, remote_session, chat
    id: str
    meta: dict[str, Any] = field(default_factory=dict[str, Any])  # the latest list entry
    segment: int = 0  # segments written
    last_message: str | None = None  # the last message recorded
    last_ledger: dict[str, Any] | None = None  # run id and ledger_sha256 of the last segment
    pending: bool = True  # seen, and not imported since
    deleted_recorded: bool = False  # a chat's deletion has its own segment
    failures: int = 0  # runs in a row it could not be read again

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def prefix(self) -> str:
        if self.source == "chat":
            name = "chat"
        else:
            surface = self.meta.get("product_surface")
            name = _safe(surface) if isinstance(surface, str) and surface else "unknown"
        return f"{name}-{_safe(self.id)}"

    def run_id(self, segment: int) -> str:
        return f"{self.prefix}-{segment}"


@dataclass
class Summary:
    """What a run did, logged per source; lists have no counts, so this shows completeness."""

    started: str
    listed: dict[str, int] = field(default_factory=dict[str, int])
    imported: dict[str, int] = field(default_factory=dict[str, int])  # ledgers written
    messages: dict[str, int] = field(default_factory=dict[str, int])
    cursors: dict[str, Any] = field(default_factory=dict[str, Any])
    skipped: list[str] = field(default_factory=list[str])  # to retry on a later run
    stuck: list[str] = field(default_factory=list[str])  # need a person to look
    closed: list[str] = field(default_factory=list[str])  # left open by a killed run
    written: list[Path] = field(default_factory=list[Path])
    last_request_id: str | None = None

    @property
    def ok(self) -> bool:
        return not self.stuck


class Busy(Exception):
    """Another import is running on the same folder."""


class Partial(Exception):
    """The API returned only part of a transcript."""


class Importer:
    def __init__(
        self,
        cfg: ComplianceConfig,
        root: Path,
        client: ComplianceClient,
        signer: Signer | None,
        on_written: Callable[[Path], None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg = cfg
        self.root = root
        self.client = client
        self.signer = signer
        self.on_written = on_written
        self.now = now
        self.state: dict[str, Any] = {}
        self.conversations: dict[str, Conversation] = {}
        self._blocked_orgs: set[str] = set()  # a customer-managed key that cannot be used
        self._erased: set[str] = set()  # sha256 of owner ids `seatbelt erase` removed
        self._fetched: tuple[str, dict[str, Any]] = ("", {})  # the last transcript's request
        self._request_ids: list[str] = []  # the responses the transcript came from

    # -- state --------------------------------------------------------------------------

    def _load(self) -> None:
        path = self.root / STATE
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.state = {}
        except (OSError, ValueError) as exc:
            # rebuilt from the API and the ledgers: nothing is recorded twice (run ids)
            _log.warning("state %s unreadable (%s); starting again from `since`", path, exc)
            self.state = {}
        for raw in self.state.get("conversations", {}).values():
            c = Conversation(**raw)
            self.conversations[c.key] = c

    def _save(self) -> None:
        self.state["conversations"] = {k: vars(c) for k, c in self.conversations.items()}
        path = self.root / STATE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _conversation(self, source: str, cid: str) -> Conversation:
        key = f"{source}:{cid}"
        if key not in self.conversations:
            self.conversations[key] = Conversation(source=source, id=cid)
        return self.conversations[key]

    # -- a pass ---------------------------------------------------------------------------

    def run(self) -> Summary:
        """One pass. Raises `Busy` when another import holds the folder: two at once would
        close each other's open ledgers."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / LOCK).open("wb") as handle:
            if not try_lock(handle):
                raise Busy(f"another import, or an erase, is running on {self.root}")
            return self._run()

    def _run(self) -> Summary:
        start = self.now()
        summary = Summary(started=_iso(start))
        # raises when the list cannot be read: better no import than an erased person restored
        self._erased = read_erased(self.root)
        closed = close_open_chains(
            self.root, self.signer, only=_imported, reason=KILLED
        )  # a run killed before signing: close it, and carry on after its last message
        summary.closed = [p.stem for p in closed]
        self._load()
        sources = set(self.cfg.sources)
        try:
            if "local_sessions" in sources:
                self._local(start, summary)
            if "remote_sessions" in sources:
                self._remote(start, summary)
            if "chats" in sources:
                self._chats(start, summary)
        finally:
            self._save()
        ids = self.client.request_ids
        summary.last_request_id = ids[-1] if ids else None
        return summary

    def _wanted(self, meta: dict[str, Any]) -> bool:
        surfaces = self.cfg.surfaces
        wanted = surfaces is None or meta.get("product_surface") in surfaces
        return wanted and not self._suppressed(meta)

    def _suppressed(self, meta: dict[str, Any]) -> bool:
        """Owned by, or started by, a person whose ledgers `seatbelt erase` removed."""
        owner = as_dict(meta.get("user")) or as_dict(meta.get("started_by_user"))
        oid = owner.get("id")
        return isinstance(oid, str) and id_hash(oid) in self._erased

    def _settled(self, meta: dict[str, Any], start: datetime) -> bool:
        updated = _when(meta.get("updated_at"))
        return updated is not None and start - updated >= timedelta(seconds=self.cfg.settle)

    # -- local sessions -------------------------------------------------------------------

    def _local(self, start: datetime, summary: Summary) -> None:
        last = _when(self.state.get("local", {}).get("last_run_start"))
        params: dict[str, Any] = {"limit": 500}
        if last is not None:
            bound: datetime | None = last - timedelta(seconds=self.cfg.overlap)
        else:
            bound = self.cfg.since
        if bound is not None:
            params["updated_at.gte"] = _iso(bound)
        summary.cursors["local_sessions"] = params.get("updated_at.gte")
        listed: set[str] = set()
        complete = True
        try:
            for page in self.client.pages(LOCAL, params):
                for meta in as_dicts(page.get("data")):
                    if not self._wanted(meta):
                        continue
                    c = self._conversation("local_session", str(meta.get("id")))
                    if meta.get("updated_at") != c.meta.get("updated_at"):
                        c.pending = True
                    c.meta = meta
                    listed.add(c.key)
        except TryLater as exc:
            _log.warning("local sessions list unavailable (%s); retried next run", exc)
            summary.skipped.append("local_sessions list")
            complete = False
        summary.listed["local_sessions"] = len(listed)
        for c in self._pending("local_session"):
            if c.key not in listed:  # quiet since it was listed: read it again
                try:
                    c.meta = self.client.get(f"{LOCAL}/{c.id}")
                except NotFound:
                    _log.info("local session %s is gone (retention or access)", c.id)
                    c.pending = False
                    continue
                except TryLater:
                    summary.skipped.append(c.key)
                    continue
                except ContentUnavailable as exc:
                    org = str(c.meta.get("organization_uuid"))
                    _log.warning("org %s: sessions unavailable (%s); skipped this run", org, exc)
                    self._blocked_orgs.add(org)
                    summary.skipped.append(c.key)
                    continue
                except ComplianceError as exc:
                    c.failures += 1
                    if c.failures < STUCK_AFTER:
                        _log.warning("%s: %s; retried next run", c.key, exc)
                        summary.skipped.append(c.key)
                    else:
                        _log.warning(
                            "%s: %s, %d runs in a row; retried each run. Check it by hand",
                            c.key,
                            exc,
                            c.failures,
                        )
                        summary.stuck.append(c.key)
                    continue
            c.failures = 0  # listed, or read again
            if self._settled(c.meta, start):
                self._import(c, summary)
        if complete:
            self.state.setdefault("local", {})["last_run_start"] = _iso(start)

    def _local_messages(self, c: Conversation) -> list[Message]:
        params = {"limit": 1000, "tool_use_input_max_bytes": FULL, "tool_result_max_bytes": FULL}
        self._fetched = (f"{LOCAL}/{c.id}/messages", params)
        return [
            local_message(m)
            for page in self.client.pages(f"{LOCAL}/{c.id}/messages", params)
            for m in as_dicts(page.get("data"))
        ]

    # -- remote sessions ------------------------------------------------------------------

    def _remote(self, start: datetime, summary: Summary) -> None:
        # sessions created since the last run, and every session still followed: the list
        # has no updated_at filter, so a followed session is found again by when it began
        last = _when(self.state.get("remote", {}).get("last_run_start"))
        bound = last - timedelta(seconds=self.cfg.overlap) if last is not None else self.cfg.since
        if bound is not None:
            for c in self._pending("remote_session", every=True):
                created = _when(c.meta.get("created_at"))
                if created is not None:
                    bound = min(bound, created)
        params: dict[str, Any] = {"limit": 500}
        if bound is not None:
            params["created_at.gte"] = _iso(bound)
        summary.cursors["remote_sessions"] = params.get("created_at.gte")
        listed: set[str] = set()
        for page in self.client.pages(REMOTE, params):
            for meta in as_dicts(page.get("data")):
                if not self._wanted(meta):
                    continue
                c = self._conversation("remote_session", str(meta.get("id")))
                changed = (meta.get("updated_at"), meta.get("status")) != (
                    c.meta.get("updated_at"),
                    c.meta.get("status"),
                )
                if changed:
                    c.pending = True
                c.meta = meta
                listed.add(c.key)
        summary.listed["remote_sessions"] = len(listed)
        for c in self._pending("remote_session", every=True):
            if c.key not in listed:
                _log.info("remote session %s is no longer listed: deleted", c.id)
                c.pending = False
                c.meta = {**c.meta, "status": "deleted"}
                continue
            status = c.meta.get("status")
            if status == "pending":
                continue
            if c.pending and (status in FINAL or self._settled(c.meta, start)):
                self._import(c, summary)
        self.state.setdefault("remote", {})["last_run_start"] = _iso(start)

    def _remote_messages(self, c: Conversation) -> list[Message]:
        params = {"limit": 1000, "tool_use_input_max_bytes": FULL, "tool_result_max_bytes": FULL}
        self._fetched = (f"{REMOTE}/{c.id}/messages", params)
        return [
            remote_message(m)
            for page in self.client.pages(f"{REMOTE}/{c.id}/messages", params)
            for m in as_dicts(page.get("data"))
        ]

    # -- chats ----------------------------------------------------------------------------

    def _chats(self, start: datetime, summary: Summary) -> None:
        chats = self.state.setdefault("chats", {})
        params: dict[str, Any] = {"order_by": "updated_at", "limit": 1000}
        if chats.get("after_id"):
            params["after_id"] = chats["after_id"]
        elif self.cfg.since is not None:  # time bounds only on the first walk: they must
            params["updated_at.gte"] = _iso(self.cfg.since)  # match the cursor's sort key
        summary.cursors["chats"] = chats.get("after_id")
        listed = 0
        while True:
            page = self.client.get(CHATS, params)
            for meta in as_dicts(page.get("data")):
                if self._suppressed(meta):
                    continue
                c = self._conversation("chat", str(meta.get("id")))
                c.meta = meta
                c.pending = True
                listed += 1
            if page.get("last_id"):
                chats["after_id"] = page["last_id"]
            if not page.get("has_more") or not page.get("last_id"):
                break
            params = {"order_by": "updated_at", "limit": 1000, "after_id": page["last_id"]}
        summary.listed["chats"] = listed
        for c in self._pending("chat"):
            if c.meta.get("deleted_at"):
                self._deleted(c, summary)
            elif self._settled(c.meta, start):
                self._import(c, summary)

    def _chat_messages(self, c: Conversation) -> list[Message]:
        params = {"tool_use_input_max_chars": FULL, "tool_result_max_chars": FULL}
        self._fetched = (f"{CHATS}/{c.id}/messages", params)
        chat = self.client.get(f"{CHATS}/{c.id}/messages", params)
        if chat.get("has_more"):  # the whole chat is documented to come back in one response
            raise Partial(f"{CHATS}/{c.id}/messages returned has_more: true")
        fresh = {k: v for k, v in chat.items() if k not in ("chat_messages", "has_more")}
        c.meta = {**c.meta, **fresh}  # the chat's metadata, as of these messages
        return [chat_message(m) for m in as_dicts(chat.get("chat_messages"))]

    # -- writing --------------------------------------------------------------------------

    def _pending(self, source: str, every: bool = False) -> list[Conversation]:
        """Conversations of `source` waiting to be imported; with `every`, remote sessions
        still followed as well: not final, and updated within `FOLLOW`."""
        out: list[Conversation] = []
        now = self.now()
        for c in list(self.conversations.values()):
            if c.source != source:
                continue
            if not every:
                if c.pending:
                    out.append(c)
                continue
            status = c.meta.get("status")
            updated = _when(c.meta.get("updated_at"))
            fresh = updated is not None and now - updated < FOLLOW
            if c.pending or (status not in FINAL | {"deleted"} and fresh):
                out.append(c)
        return out

    def _adopt(self, c: Conversation) -> None:
        """Take on segments already on disk that the state does not know: written by a run
        killed before it saved its state, or before the state file was lost."""
        while (path := self.root / f"{c.run_id(c.segment + 1)}.jsonl").exists():
            try:
                events = list(read_events(path))
            except (OSError, LedgerError) as exc:
                raise LedgerError(f"{path}: {exc}") from exc
            c.segment += 1
            ids = [
                str(e.attrs["compliance.message_id"])
                for e in events
                if "compliance.message_id" in e.attrs
            ]
            end = events[-1] if events else None
            ok = end is not None and end.kind is Kind.RUN_END and end.attrs.get("run.ok") is True
            if ids and not ok:
                # killed or failed: its last message may be only partly written (one message
                # is several events), so the next segment records that message again, whole
                ids = [m for m in ids if m != ids[-1]]
            if ids:
                c.last_message = ids[-1]
            c.last_ledger = _ledger_ref(path)
            if any(e.attrs.get("compliance.deleted_at") for e in events[:1]):
                c.deleted_recorded = True

    def _messages(self, c: Conversation) -> list[Message]:
        if c.source == "local_session":
            return self._local_messages(c)
        if c.source == "remote_session":
            return self._remote_messages(c)
        return self._chat_messages(c)

    def _import(self, c: Conversation, summary: Summary) -> None:
        if self._suppressed(c.meta):
            c.pending = False
            return
        org = str(c.meta.get("organization_uuid"))
        if org in self._blocked_orgs:
            summary.skipped.append(c.key)
            return
        mark = len(self.client.request_ids)
        try:
            self._adopt(c)
            messages = self._messages(c)
        except LedgerError as exc:
            _log.warning("%s: a segment on disk cannot be read (%s). Check it by hand", c.key, exc)
            summary.stuck.append(c.key)
            return
        except Partial as exc:
            _log.warning("%s: %s; nothing written, retried next run. Check it by hand", c.key, exc)
            summary.stuck.append(c.key)
            return
        except NotFound:
            if c.source == "remote_session" and c.meta.get("status") == "pending":
                return
            _log.info("%s is gone: nothing more to import", c.key)
            c.pending = False
            if c.source == "remote_session":
                c.meta = {**c.meta, "status": "deleted"}
            return
        except TryLater:
            summary.skipped.append(c.key)
            return
        except ContentUnavailable as exc:
            _log.warning("org %s: transcripts unavailable (%s); skipped this run", org, exc)
            self._blocked_orgs.add(org)
            summary.skipped.append(c.key)
            return
        except ComplianceError as exc:
            _log.warning("%s: %s; retried next run", c.key, exc)
            summary.skipped.append(c.key)
            return
        self._request_ids = self.client.request_ids[mark:]
        if c.source == "chat" and c.meta.get("deleted_at"):  # deleted since it was listed
            self._deleted(c, summary)
            return
        new = self._new(c, messages)
        if new is None:
            _log.warning(
                "%s: its last recorded message %s is no longer in the transcript, so what is "
                "new cannot be told; nothing written. Check it by hand",
                c.key,
                c.last_message,
            )
            summary.stuck.append(c.key)
            c.pending = False
            return
        if new:
            self._write(c, new, summary)
        c.pending = False
        self._save()  # a crash from here on costs at most this conversation's next check

    def _new(self, c: Conversation, messages: list[Message]) -> list[Message] | None:
        """The messages after the last one recorded; None when that one is gone and what
        is new cannot be told."""
        if c.last_message is None:
            return messages
        ids = [m.id for m in messages]
        if c.last_message in ids:
            return messages[ids.index(c.last_message) + 1 :]
        if messages and is_retention_placeholder(messages[0]):
            # retention removes the oldest turns first: when the last one recorded has aged
            # out, every turn still kept came after it. The placeholder (a new id each time
            # more turns age out) stands for turns already recorded, so it is not recorded
            return messages[1:]
        return None

    def _metadata(self, c: Conversation, segment: int) -> dict[str, Any]:
        m = c.meta
        user = as_dict(m.get("user"))
        started = as_dict(m.get("started_by_user"))
        owner = user or started
        meta: dict[str, Any] = {
            "principal.id": str(owner.get("id") or "unknown"),
            "principal.auth": "compliance_api",
            "compliance.source": c.source,
            "compliance.id": c.id,
            "compliance.segment": segment,
            "compliance.previous": c.last_ledger,
            "compliance.endpoint": self._fetched[0] or None,
            "compliance.query": self._fetched[1] or None,
            "compliance.request_ids": list(self._request_ids),
        }
        if isinstance(owner.get("email_address"), str):
            meta["principal.name"] = owner["email_address"]
        if c.source == "chat" and isinstance(m.get("name"), str) and m.get("name"):
            meta["run.name"] = m["name"]
        for key, attr in (
            ("organization_uuid", "compliance.organization_uuid"),
            ("product_surface", "compliance.product_surface"),
            ("workspace_id", "compliance.workspace_id"),
            ("project_id", "compliance.project_id"),
            ("claude_project_id", "compliance.project_id"),
            ("agent_id", "compliance.agent_id"),
            ("created_at", "compliance.created_at"),
            ("updated_at", "compliance.updated_at"),
            ("status", "compliance.status"),
            ("deleted_at", "compliance.deleted_at"),
        ):
            if m.get(key) is not None:
                meta[attr] = m[key]
        if started:
            meta["compliance.started_by"] = started
        return meta

    def _write(self, c: Conversation, messages: Iterable[Message], summary: Summary) -> None:
        segment = c.segment + 1
        run_id = c.run_id(segment)
        user = as_dict(c.meta.get("user")) or as_dict(c.meta.get("started_by_user"))
        owner = str(user.get("id") or "unknown")
        chat_model = c.meta.get("model") if c.source == "chat" else None
        with Recorder.start(
            self.root,
            agent_id=f"compliance:{c.prefix.split('-', 1)[0]}",
            run_id=run_id,
            metadata=self._metadata(c, segment),
            signer=self.signer,
        ) as rec:
            writer = Writer(rec, owner, chat_model if isinstance(chat_model, str) else None)
            for msg in messages:
                writer.write(msg)
        path = self.root / f"{run_id}.jsonl"
        c.segment = segment
        c.last_message = writer.last_message or c.last_message
        c.last_ledger = _ledger_ref(path)
        summary.imported[c.source] = summary.imported.get(c.source, 0) + 1
        summary.messages[c.source] = summary.messages.get(c.source, 0) + writer.messages
        summary.written.append(path)
        if self.on_written is not None:
            self.on_written(path)

    def _deleted(self, c: Conversation, summary: Summary) -> None:
        """A chat deleted in claude.ai: a segment with no messages records when."""
        try:
            self._adopt(c)
        except LedgerError as exc:
            _log.warning("%s: a segment on disk cannot be read (%s). Check it by hand", c.key, exc)
            summary.stuck.append(c.key)
            return
        if not c.deleted_recorded:
            self._write(c, [], summary)
            c.deleted_recorded = True
        c.pending = False
        self._save()


def _imported(events: list[Any]) -> bool:
    return "compliance.id" in events[0].attrs


def _ledger_ref(path: Path) -> dict[str, Any]:
    ref: dict[str, Any] = {"run_id": path.stem, "ledger_sha256": None}
    with contextlib.suppress(OSError, ValueError):  # unsigned: no key configured
        ref["ledger_sha256"] = json.loads(sidecar(path).read_text()).get("ledger_sha256")
    return ref
