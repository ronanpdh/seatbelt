"""A run as one HTML page: what `seatbelt reconstruct` prints, for a browser, to keep or send.

The page is a view, not evidence. It is rendered only from a ledger that verifies, says what
was checked when it was rendered, and names the command that checks the ledger again: anyone
can edit the page, and only the ledger and its signature prove anything.

Everything in a ledger is untrusted: a prompt, a model's answer or a tool's output can hold
HTML. The template escapes every value (Jinja2 with autoescaping on, which a plain Jinja2
environment leaves off), every string goes through `printable` as in the terminal, and a
Content-Security-Policy as the head's first element allows no script and no request, for a
value that ever escaped the template. Design: docs/plans/2026-10-09-html-run-report-design.md.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shlex
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

import jinja2

from seatbelt import __version__
from seatbelt.attest.manifest import Manifest, sidecar
from seatbelt.ledger.events import Event, Kind
from seatbelt.ledger.store import LedgerError, parse_events
from seatbelt.report import page_path
from seatbelt.report.fleet import Usage, count_response, counted, response_model
from seatbelt.report.timeline import summary
from seatbelt.terminal import printable
from seatbelt.verify.attest import Attestation, verify_attestation
from seatbelt.verify.chain import verify_events

DETAIL_LIMIT = 64 * 1024  # bytes of one event's detail on the page; the ledger keeps it all
# of a longer one, its start and its end: a request's newest turn is at the end of its messages
DETAIL_HEAD = 8 * 1024
TEMPLATE = "run.html"


class PageError(Exception):
    """A ledger that gets no page: unreadable, broken, forged, or unsigned when a key is given."""


@dataclass(frozen=True)
class Row:
    seq: int
    time: str
    kind: str
    css: str  # a class from the event's kind: one of a fixed set, never ledger text
    actor: str
    what: str
    denied: bool
    failed: bool
    id: str
    parent: str
    hash: str
    prev_hash: str
    detail: str
    cut: int  # bytes of detail left out, from the middle
    detail_end: str  # what follows the part left out


@dataclass(frozen=True)
class Refusal:
    seq: int
    rule: str
    reason: str


@dataclass(frozen=True)
class Failure:
    seq: int
    kind: str
    error: str


@dataclass
class Page:
    """What the template shows. Every string in it is already `printable`."""

    title: str
    run_id: str
    name: str
    principal: str
    client: str
    agent: str
    started: str
    ended: str
    duration: str
    outcome: str  # ok, failed or incomplete
    outcome_reason: str
    events: int
    complete: bool
    signature: str  # an Attestation value
    signature_note: str
    final_hash: str
    ledger_sha256: str
    ledger_file: str
    verify_command: str
    rendered: str
    version: str
    models: dict[str, Usage] = field(default_factory=dict[str, Usage])
    total: Usage = field(default_factory=Usage)
    tools: dict[str, int] = field(default_factory=dict[str, int])
    refusals: list[Refusal] = field(default_factory=list[Refusal])
    failures: list[Failure] = field(default_factory=list[Failure])
    rows: list[Row] = field(default_factory=list[Row])


_CSS = {kind: kind.value.replace(".", "-") for kind in Kind}


def _text(value: object) -> str:
    return printable(str(value)) if value is not None else ""


def _when(ts: datetime) -> str:
    """Local time with its UTC offset, to the second."""
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _duration(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _detail(event: Event) -> tuple[str, int, str]:
    """The event's attributes as JSON: whole up to DETAIL_LIMIT bytes (indented when that fits
    too), else, as compact JSON, its first DETAIL_HEAD bytes, how many bytes from the middle
    are left out, and the rest of the limit from its end. (Indenting makes `json` use its
    pure-Python encoder, seconds on a long session's requests, which are cut anyway.) Cut
    before `printable`, which then makes C1 controls and bidirectional overrides visible;
    JSON escapes C0 controls."""
    raw = json.dumps(event.attrs, ensure_ascii=False, default=str)
    data = raw.encode()
    if len(data) <= DETAIL_LIMIT:  # whole: indented if that fits too, else compact
        pretty = json.dumps(event.attrs, indent=2, ensure_ascii=False, default=str)
        return printable(pretty if len(pretty.encode()) <= DETAIL_LIMIT else raw), 0, ""
    head = data[:DETAIL_HEAD].decode(errors="ignore")
    end = data[len(data) - (DETAIL_LIMIT - DETAIL_HEAD) :].decode(errors="ignore")
    left = len(data) - len(head.encode()) - len(end.encode())
    return printable(head), left, printable(end)


def _row(event: Event) -> Row:
    a = event.attrs
    actor = f"{event.actor.type}:{event.actor.id}"
    if event.actor.version:
        actor += f"@{event.actor.version}"
    detail, cut, detail_end = _detail(event)
    return Row(
        seq=event.seq,
        time=event.ts.astimezone().strftime("%H:%M:%S.%f")[:-3],
        kind=event.kind.value,
        css=_CSS[event.kind],
        actor=printable(actor),
        what=summary(event),
        denied=event.kind is Kind.POLICY_CHECK and a.get("policy.allowed") is False,
        failed=bool(a.get("error")) or (event.kind is Kind.RUN_END and a.get("run.ok") is False),
        id=printable(event.id),
        parent=printable(event.parent_id or ""),
        hash=event.hash,
        prev_hash=event.prev_hash,
        detail=detail,
        cut=cut,
        detail_end=detail_end,
    )


def _verify_command(ledger: Path, pubkey: Path | None) -> str:
    command = f"seatbelt verify {shlex.quote(str(ledger))}"
    if pubkey is not None:
        command += f" --pubkey {shlex.quote(str(pubkey))}"
    return printable(command)


_SIGNATURE_NOTES = {
    Attestation.ATTESTED: "signed, and the signature verifies with the given key",
    Attestation.UNCHECKED: "a signature beside it matches the ledger, but no key was given to "
    "check who made it",
    Attestation.UNATTESTED: "no signature beside it",
}


def _signed_sha256(ledger: Path) -> str | None:
    """The ledger SHA-256 its signature pins, which the attestation check compared with the
    file as it read it."""
    try:
        return Manifest.model_validate_json(sidecar(ledger).read_bytes()).ledger_sha256
    except (OSError, ValueError):
        return None


def build(ledger: Path, pubkey: Path | None = None, now: datetime | None = None) -> Page:
    """What the page for `ledger` shows, after the checks `seatbelt reconstruct` makes. Raises
    PageError for a ledger that cannot be read, a broken chain, a forged signature, or, with
    `pubkey`, no signature. AttestError for a key file that cannot be read."""
    try:  # one read: the page shows, hashes and checks the same bytes
        raw = ledger.read_bytes()
        events = list(parse_events(raw, ledger))
    except (OSError, UnicodeDecodeError, LedgerError) as exc:
        raise PageError(f"{ledger}: {exc}") from exc
    sha256 = hashlib.sha256(raw).hexdigest()
    if not events:
        raise PageError(f"{ledger}: no events")
    verdict = verify_events(events)
    if not verdict.ok:
        where = "" if verdict.first_bad_seq is None else f" at seq {verdict.first_bad_seq}"
        raise PageError(f"{ledger}: chain broken{where}: {verdict.reason}")
    att = verify_attestation(ledger, pubkey)
    if att.status is Attestation.FORGED:
        raise PageError(f"{ledger}: forged: {att.reason}")
    if att.status is Attestation.UNATTESTED and pubkey is not None:
        raise PageError(f"{ledger}: no signature to check against the key")
    if att.status is not Attestation.UNATTESTED and _signed_sha256(ledger) != sha256:
        # the check above read the file again: it was written to in between
        raise PageError(f"{ledger}: changed while its page was being made; try again")
    first, last = events[0], events[-1]
    meta = first.attrs
    ended = last.kind is Kind.RUN_END
    if not verdict.complete:
        outcome, reason = "incomplete", "no matching run.end: still running, or cut short"
    elif last.attrs.get("run.ok") is False:
        outcome, reason = "failed", _text(last.attrs.get("run.error"))
    else:
        outcome, reason = "ok", ""
    name = meta.get("run.name")
    page = Page(
        title=_text(name if isinstance(name, str) and name else first.run_id),
        run_id=_text(first.run_id),
        name=_text(name) if isinstance(name, str) else "",
        principal=_text(meta.get("principal.id")),
        client=_text(meta.get("client.user_agent")),
        agent=_text(f"{first.actor.id} {first.actor.version or ''}".strip()),
        started=_when(first.ts),
        ended=_when(last.ts) if ended else "",
        duration=_duration(first.ts, last.ts),
        outcome=outcome,
        outcome_reason=reason,
        events=verdict.events,
        complete=verdict.complete,
        signature=att.status.value,
        signature_note=_SIGNATURE_NOTES.get(att.status, ""),
        final_hash=last.hash,
        ledger_sha256=sha256,
        ledger_file=printable(ledger.name),
        verify_command=_verify_command(ledger, pubkey),
        rendered=_when(now or datetime.now().astimezone()),
        version=__version__,
    )
    models: dict[str, Usage] = {}
    tools: Counter[str] = Counter()
    for e in events:
        a = e.attrs
        if counted(e):
            count_response(models.setdefault(printable(response_model(e)), Usage()), e)
            count_response(page.total, e)
        if e.kind is Kind.TOOL_CALL:
            tools[_text(a.get("gen_ai.tool.name"))] += 1
        elif e.kind is Kind.POLICY_CHECK and a.get("policy.allowed") is False:
            page.refusals.append(Refusal(e.seq, _text(e.actor.id), _text(a.get("policy.reason"))))
        if a.get("error"):
            page.failures.append(Failure(e.seq, e.kind.value, _text(a.get("error"))))
        page.rows.append(_row(e))
    page.models = dict(sorted(models.items()))
    page.tools = dict(sorted(tools.items()))
    return page


@functools.cache
def _template() -> jinja2.Template:
    # autoescape=True for every template: off is Jinja2's default, and select_autoescape goes by
    # a file's extension. StrictUndefined: a misspelt name fails a test instead of printing ""
    env = jinja2.Environment(
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    source = resources.files(__package__).joinpath("templates", TEMPLATE).read_text("utf-8")
    return env.from_string(source)


def render(ledger: Path, pubkey: Path | None = None, now: datetime | None = None) -> str:
    """The page for `ledger` as HTML. Raises as `build` does."""
    return _template().render(page=build(ledger, pubkey, now), detail_limit=DETAIL_LIMIT)


def write_page(
    ledger: Path, pubkey: Path | None = None, out: Path | None = None, now: datetime | None = None
) -> Path:
    """Write the page for `ledger` to `out` (default: beside the ledger), mode 0600, replacing
    any page there whole: it is written to a temporary file and renamed into place, so nobody
    reads half a page. Returns where it went. Raises as `build` does, and OSError."""
    html = render(ledger, pubkey, now)
    out = out or page_path(ledger)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)  # 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(html)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out
