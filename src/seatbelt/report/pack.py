"""Evidence pack: one zip of ledgers, attestations and findings, bound by a signed manifest."""

from __future__ import annotations

import hashlib
import re
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from seatbelt import __version__
from seatbelt.attest.manifest import Signed, sidecar
from seatbelt.attest.sign import Signer, load_public_key
from seatbelt.ledger.store import LedgerError, read_events
from seatbelt.record.recorder import RUN_ID
from seatbelt.scenarios.model import corpus_sha256
from seatbelt.scenarios.runner import Report
from seatbelt.verify.attest import (
    Attestation,
    check_attestation,
    verify_attestation,
    verify_signature,
)
from seatbelt.verify.chain import verify_file

PACK_VERSION = 1
MANIFEST = "pack.json"
_MEMBER = re.compile(r"runs/[^/]+\.(jsonl|attest\.json)|findings\.json|corpus/[^/]+\.yaml")
_EPOCH = (1980, 1, 1, 0, 0, 0)  # zip's earliest timestamp; fixed so builds are byte-identical
# A pack is untrusted input: bound what verifying it may inflate, in memory and on disk
_MAX_MANIFEST = 16 << 20
_MAX_MEMBER = 2 << 30
_MAX_TOTAL = 8 << 30
_MAX_RATIO = 200  # uncompressed / compressed, for members above _RATIO_FLOOR
_RATIO_FLOOR = 16 << 20
_CHUNK = 1 << 20


class PackError(Exception):
    """The pack cannot be built. The message names the file."""


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    bytes: int


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=f"^{RUN_ID.pattern}$")  # names runs/<run_id>.jsonl
    events: int
    complete: bool
    attested: bool


class PackManifest(Signed):
    pack_version: int
    harness: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    members: list[Member]
    runs: list[RunSummary]
    findings: int | None = None
    corpus_sha256: str | None = None


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PackError(f"{path}: {exc}") from exc


def build(
    runs_dir: Path, out: Path, *, signer: Signer | None = None, corpus: Path | None = None
) -> PackManifest:
    # ponytail: every blob is held in memory; stream members when ledgers exceed RAM
    ledgers = sorted(runs_dir.glob("*.jsonl"))
    if not ledgers:
        raise PackError(f"{runs_dir}: no ledgers (*.jsonl) found")
    blobs: list[tuple[str, bytes]] = []
    runs: list[RunSummary] = []
    for ledger in ledgers:
        verdict = verify_file(ledger)
        if not verdict.ok:
            raise PackError(
                f"{ledger}: BROKEN at seq {verdict.first_bad_seq}: {verdict.reason}; "
                "a pack never carries a broken ledger"
            )
        if not RUN_ID.fullmatch(ledger.stem):
            raise PackError(f"{ledger}: a run id must match {RUN_ID.pattern}; rename the file")
        blobs.append((f"runs/{ledger.name}", _read(ledger)))
        side = sidecar(ledger)
        # never pack a sidecar that contradicts its ledger, nor sign over one another key made
        att = check_attestation(ledger, signer.public_key() if signer is not None else None)
        if att.status is Attestation.FORGED:
            raise PackError(f"{side}: {att.reason}")
        if side.exists():
            blobs.append((f"runs/{side.name}", _read(side)))
        runs.append(
            RunSummary(
                run_id=ledger.stem,
                events=verdict.events,
                complete=verdict.complete,
                attested=side.exists(),
            )
        )
    findings: int | None = None
    corpus_digest: str | None = None
    report_path = runs_dir / "findings.json"
    if report_path.exists():
        raw = _read(report_path)
        try:
            report = Report.model_validate_json(raw)
        except ValidationError as exc:
            raise PackError(f"{report_path}: not a findings report: {exc}") from exc
        findings, corpus_digest = len(report.findings), report.corpus_sha256
        blobs.append(("findings.json", raw))
    if corpus is not None:
        if corpus_digest is None:
            raise PackError(
                f"{runs_dir} has no findings.json, so nothing names a corpus to include"
            )
        if corpus_sha256(corpus) != corpus_digest:
            raise PackError(f"{corpus}: hash does not match findings.json ({corpus_digest})")
        blobs += [(f"corpus/{p.name}", _read(p)) for p in sorted(corpus.glob("*.yaml"))]
    blobs.sort()
    members = [
        Member(path=name, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        for name, data in blobs
    ]
    manifest = PackManifest(
        pack_version=PACK_VERSION,
        harness=f"seatbelt {__version__}",
        members=members,
        runs=runs,
        findings=findings,
        corpus_sha256=corpus_digest,
    )
    if signer is not None:
        manifest = signer.sign(manifest)
    try:
        with zipfile.ZipFile(out, "x", zipfile.ZIP_DEFLATED) as zf:
            for name, data in blobs:
                _add(zf, name, data)
            _add(zf, MANIFEST, manifest.model_dump_json(indent=2).encode() + b"\n")
    except FileExistsError as exc:
        raise PackError(f"{out} exists; refusing to overwrite") from exc
    except OSError as exc:
        raise PackError(f"{out}: {exc.strerror}") from exc
    return manifest


def _add(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


class PackStatus(StrEnum):
    ATTESTED = "attested"
    UNSIGNED = "unsigned"  # empty signature
    UNCHECKED = "unchecked"  # signed, no public key supplied
    FORGED = "forged"


@dataclass(frozen=True)
class LedgerStatus:
    run_id: str
    chain: str  # ok | incomplete | broken
    attestation: Attestation
    reason: str | None = None


@dataclass(frozen=True)
class PackVerdict:
    status: PackStatus
    ledgers: list[LedgerStatus] = field(default_factory=list[LedgerStatus])
    reason: str | None = None
    keyed: bool = False  # a public key was given: no signature is a failure, as in `verify`

    @property
    def unsigned(self) -> bool:
        """A key was given but the pack carries no signature (ADR 0002)."""
        return self.keyed and self.status is PackStatus.UNSIGNED

    @property
    def unattested(self) -> list[str]:
        """Run ids with no sidecar although a key was given: deleting it is the cheapest tamper."""
        if not self.keyed:
            return []
        return [s.run_id for s in self.ledgers if s.attestation is Attestation.UNATTESTED]

    @property
    def ok(self) -> bool:
        return (
            self.status is not PackStatus.FORGED
            and all(s.chain != "broken" for s in self.ledgers)
            and not self.unsigned
            and not self.unattested
        )


def _safe(name: str) -> bool:
    p = PurePosixPath(name)
    return (
        not p.is_absolute()
        and ".." not in p.parts
        and "\\" not in name
        and not name.startswith("/")
    )


def _forged(reason: str) -> PackVerdict:
    return PackVerdict(PackStatus.FORGED, reason=reason)


def _inside(root: Path, path: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def verify_pack(path: Path, pubkey: Path | None = None) -> PackVerdict:
    key = load_public_key(pubkey) if pubkey is not None else None  # a typo fails even unsigned
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        return _forged(f"{path}: not a zip: {exc}")
    except OSError as exc:
        return _forged(f"{path}: {exc.strerror}")
    with zf:
        names = zf.namelist()
        seen: set[str] = set()
        for n in names:
            if n in seen:
                return _forged(f"duplicate entry {n!r}")
            seen.add(n)
        unsafe = [n for n in names if not _safe(n)]
        if unsafe:
            return _forged(f"unsafe member path {unsafe[0]!r}")
        for info in zf.infolist():
            if info.flag_bits & 0x1:
                return _forged(f"{info.filename}: encrypted members are not allowed")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                return _forged(f"{info.filename}: compression method {info.compress_type}")
        if MANIFEST not in names:
            return _forged(f"no {MANIFEST} in the pack")
        if zf.getinfo(MANIFEST).file_size > _MAX_MANIFEST:
            return _forged(f"{MANIFEST} is larger than {_MAX_MANIFEST} bytes")
        try:
            with zf.open(MANIFEST) as fh:
                raw = fh.read(_MAX_MANIFEST + 1)
            if len(raw) > _MAX_MANIFEST:
                return _forged(f"{MANIFEST} is larger than {_MAX_MANIFEST} bytes")
            manifest = PackManifest.model_validate_json(raw)
        except ValidationError as exc:
            return _forged(f"{MANIFEST} is not a pack manifest: {exc}")
        except _CORRUPT as exc:
            return _forged(f"{MANIFEST}: {exc}")
        if manifest.pack_version != PACK_VERSION:
            return _forged(f"unsupported pack_version {manifest.pack_version}")
        if not manifest.signature:
            status = PackStatus.UNSIGNED
        elif key is None:
            status = PackStatus.UNCHECKED
        elif verify_signature(key, manifest):
            status = PackStatus.ATTESTED
        else:
            return _forged("signature does not verify with the given key")
        for m in manifest.members:
            if not _MEMBER.fullmatch(m.path):
                return _forged(f"{m.path}: not a pack member path")
        expected = {m.path for m in manifest.members}
        actual = set(names) - {MANIFEST}
        if stray := sorted(actual - expected):
            return _forged(f"members not in manifest: {stray}")
        if missing := sorted(expected - actual):
            return _forged(f"members missing from the zip: {missing}")
        if sum(max(m.bytes, 0) for m in manifest.members) > _MAX_TOTAL:
            return _forged(f"members add up to more than {_MAX_TOTAL} bytes")
        for m in manifest.members:
            if reason := _oversized(zf.getinfo(m.path), m):
                return _forged(f"{m.path}: {reason}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for m in manifest.members:
                if reason := _extract(zf, m, root):
                    return _forged(f"{m.path}: {reason}")
            verdict = _verify_contents(root, manifest, pubkey, status)
        return replace(verdict, keyed=key is not None)


_CORRUPT = (zipfile.BadZipFile, zlib.error, OSError, RuntimeError, NotImplementedError, EOFError)


def _oversized(info: zipfile.ZipInfo, m: Member) -> str | None:
    if m.bytes > _MAX_MEMBER:
        return f"larger than {_MAX_MEMBER} bytes"
    if info.file_size > _RATIO_FLOOR and info.file_size > _MAX_RATIO * max(info.compress_size, 1):
        return f"compressed more than {_MAX_RATIO}:1"
    return None


def _extract(zf: zipfile.ZipFile, m: Member, root: Path) -> str | None:
    """Inflate one member to `root`, hashing it on the way, and never past its declared size."""
    if zf.getinfo(m.path).file_size != m.bytes:  # the central directory, before any inflating
        return "size does not match the manifest"
    dest = root / m.path
    if not _inside(root, dest):
        return "resolves outside the pack"
    digest, size = hashlib.sha256(), 0
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m.path) as src, dest.open("xb") as out:
            while size <= m.bytes and (chunk := src.read(min(_CHUNK, m.bytes + 1 - size))):
                size += len(chunk)
                digest.update(chunk)
                out.write(chunk)
    except _CORRUPT as exc:
        return str(exc)
    if size != m.bytes or digest.hexdigest() != m.sha256:
        return "content does not match the manifest"
    return None


def _event_ids(ledger: Path) -> set[str]:
    try:
        return {e.id for e in read_events(ledger)}
    except (LedgerError, OSError, UnicodeDecodeError):
        return set()


def _verify_contents(
    root: Path, manifest: PackManifest, pubkey: Path | None, status: PackStatus
) -> PackVerdict:
    listed = {r.run_id for r in manifest.runs}
    if len(listed) != len(manifest.runs):
        return _forged("a run is listed twice under runs")
    for m in manifest.members:  # a ledger or sidecar, and the run it belongs to
        stem = m.path[5:-12] if m.path.endswith(".attest.json") else m.path[5:-6]
        if m.path.startswith("runs/") and stem not in listed:
            return _forged(f"{m.path} is not listed under runs")
    members = {m.path for m in manifest.members}
    ledgers: list[LedgerStatus] = []
    for run in manifest.runs:
        ledger = root / "runs" / f"{run.run_id}.jsonl"
        if f"runs/{run.run_id}.jsonl" not in members or not _inside(root, ledger):
            return _forged(f"runs/{run.run_id}.jsonl listed under runs but absent")
        chain = verify_file(ledger)
        att = verify_attestation(ledger, pubkey)
        if att.status is Attestation.FORGED:
            return _forged(f"{run.run_id}: {att.reason}")
        summary = (chain.events, chain.complete, att.status is not Attestation.UNATTESTED)
        if chain.ok and summary != (run.events, run.complete, run.attested):
            return _forged(f"{run.run_id}: run summary does not match the ledger")
        state = "broken" if not chain.ok else ("ok" if chain.complete else "incomplete")
        ledgers.append(LedgerStatus(run.run_id, state, att.status, chain.reason or att.reason))
    report_path = root / "findings.json"
    if report_path.exists():
        try:
            report = Report.model_validate_json(report_path.read_bytes())
        except ValidationError as exc:
            return _forged(f"findings.json is not a findings report: {exc}")
        if (len(report.findings), report.corpus_sha256) != (
            manifest.findings,
            manifest.corpus_sha256,
        ):
            return _forged("findings.json does not match the manifest")
        known: dict[str, set[str]] = {}
        for f in report.findings:
            if not RUN_ID.fullmatch(f.scenario_id):
                return _forged(f"finding {f.scenario_id!r}: not a run id")
            if f.scenario_id not in known:
                known[f.scenario_id] = _event_ids(root / "runs" / f"{f.scenario_id}.jsonl")
            if not set(f.evidence) <= known[f.scenario_id]:
                return _forged(
                    f"finding {f.scenario_id} / {f.check}: evidence is not in its ledger"
                )
    elif manifest.findings is not None:
        return _forged("manifest counts findings but findings.json is absent")
    corpus = root / "corpus"
    if corpus.exists():
        if manifest.corpus_sha256 is None:
            return _forged("corpus/ is present but the manifest names no corpus_sha256")
        if corpus_sha256(corpus) != manifest.corpus_sha256:
            return _forged("corpus does not match corpus_sha256")
    return PackVerdict(status, ledgers)
