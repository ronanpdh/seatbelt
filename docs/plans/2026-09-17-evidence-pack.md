# Evidence Pack Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **No git write commands** (commit, add, push, stash, checkout, restore). The user commits manually.

**Goal:** `seatbelt pack` bundles a runs directory into one zip bound by a signed manifest; `seatbelt verify-pack` re-checks everything offline. Per `docs/plans/2026-09-17-evidence-pack-design.md`.

**Architecture:** `seatbelt.attest.manifest.Signed` becomes the shared base for anything Ed25519-signed (attestation `Manifest` and the new `PackManifest`), with `verify_signature` in `seatbelt.verify.attest`. `seatbelt.report.pack` holds `build` and `verify_pack`. CLI adds `pack` and `verify-pack`.

**Tech Stack:** stdlib `zipfile`, `hashlib`, `tempfile`; Pydantic; Typer; existing `cryptography` signer. No new dependencies.

One deviation from the design doc: the manifest carries `created_at`, so two builds differ in `pack.json`; every other member is byte-identical (fixed zip timestamps, sorted order). The determinism test compares member CRCs, not whole files. Also `RunSummary.ok` is named `complete` to avoid confusion with `run.ok`.

Commands: `uv run pytest tests/unit/test_pack.py -v`, `uv run ruff check . && uv run ruff format .`, `uv run pyright`.

---

### Task 1: `Signed` base, `verify_signature`, pack models, `build`

**Files:**
- Modify: `src/seatbelt/attest/manifest.py`, `src/seatbelt/attest/sign.py`, `src/seatbelt/verify/attest.py`
- Create: `src/seatbelt/report/pack.py`, `tests/unit/test_pack.py`

**Step 1: refactor the signer to a shared base (no behaviour change; `tests/unit/test_attest.py` must stay green).**

In `attest/manifest.py` add before `Manifest`:

```python
class Signed(BaseModel):
    """Base for anything the Ed25519 key signs: canonical form is everything but `signature`."""

    model_config = ConfigDict(extra="forbid")

    public_key: str = ""  # informational; trust comes from the verifier's own key file
    signature: str = ""

    def canonical(self) -> bytes:
        data = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
```

Make `Manifest(Signed)` and delete its own `model_config`, `public_key`, `signature` and `canonical`. In `attest/sign.py` change the signature of `Signer.sign` to `def sign[M: Signed](self, manifest: M) -> M:` (import `Signed`); body unchanged. In `verify/attest.py` add:

```python
def verify_signature(key: Ed25519PublicKey, signed: Signed) -> bool:
    try:
        key.verify(base64.b64decode(signed.signature), signed.canonical())
    except (InvalidSignature, ValueError):
        return False
    return True
```

and use it in `verify_attestation` in place of the inline `try: key.verify(...)`. Run `uv run pytest tests/unit/test_attest.py -q`: 24 passed, unchanged.

**Step 2: failing tests** `tests/unit/test_pack.py`:

```python
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from examples.scenario_target import target
from seatbelt.attest.sign import Signer, keygen
from seatbelt.ledger.store import read_events
from seatbelt.record.recorder import Recorder
from seatbelt.report.pack import MANIFEST, PackError, PackManifest, build
from seatbelt.scenarios.runner import run

ROOT = Path(__file__).resolve().parents[2]


def _scenario_runs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A signed scenario run: (runs_dir, private key, public key)."""
    key, pub = keygen(tmp_path / "keys")
    runs = tmp_path / "runs"
    run(ROOT / "scenarios", target, runs, signer=Signer.from_file(key))
    return runs, key, pub


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_build_packs_ledgers_sidecars_findings_and_manifest_last(tmp_path: Path) -> None:
    runs, key, _ = _scenario_runs(tmp_path)
    out = tmp_path / "audit.seatbelt.zip"
    manifest = build(runs, out, signer=Signer.from_file(key), corpus=ROOT / "scenarios")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert names[-1] == MANIFEST
    assert "runs/benign-order-status.jsonl" in names
    assert "runs/benign-order-status.attest.json" in names
    assert "findings.json" in names and "corpus/benign-order-status.yaml" in names
    assert {m.path for m in manifest.members} == set(names) - {MANIFEST}
    assert manifest.findings == 1 and manifest.corpus_sha256
    assert all(r.complete and r.attested for r in manifest.runs) and len(manifest.runs) == 8
    assert manifest.signature and manifest.public_key
    members = _members(out)
    for m in manifest.members:
        assert (
            hashlib.sha256(members[m.path]).hexdigest() == m.sha256
            and len(members[m.path]) == m.bytes
        )
    assert PackManifest.model_validate_json(members[MANIFEST]) == manifest


def test_build_is_deterministic_except_the_manifest(tmp_path: Path) -> None:
    runs, _, _ = _scenario_runs(tmp_path)
    build(runs, tmp_path / "a.zip")
    build(runs, tmp_path / "b.zip")
    a, b = _members(tmp_path / "a.zip"), _members(tmp_path / "b.zip")
    assert {k: v for k, v in a.items() if k != MANIFEST} == {
        k: v for k, v in b.items() if k != MANIFEST
    }
    with zipfile.ZipFile(tmp_path / "a.zip") as zf:
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())


def test_build_refuses_broken_ledger_existing_output_and_empty_dir(tmp_path: Path) -> None:
    runs, _, _ = _scenario_runs(tmp_path)
    with pytest.raises(PackError, match="no ledgers"):
        build(tmp_path / "empty", tmp_path / "x.zip")
    out = tmp_path / "audit.zip"
    build(runs, out)
    with pytest.raises(PackError, match="exists"):
        build(runs, out)
    ledger = runs / "benign-order-status.jsonl"
    ledger.write_text(ledger.read_text().replace("delivered", "lost"))
    with pytest.raises(PackError, match="BROKEN"):
        build(runs, tmp_path / "y.zip")
    assert not (tmp_path / "y.zip").exists()


def test_build_packs_incomplete_and_unattested_ledgers_honestly(tmp_path: Path) -> None:
    with Recorder.start(tmp_path / "runs", agent_id="bot", run_id="plain") as rec:
        rec.outcome("done", success=True)
    ledger = tmp_path / "runs" / "plain.jsonl"
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-1]) + "\n")
    manifest = build(tmp_path / "runs", tmp_path / "p.zip")
    assert manifest.runs[0].model_dump() == {
        "run_id": "plain",
        "events": 2,
        "complete": False,
        "attested": False,
    }
    assert manifest.findings is None and manifest.corpus_sha256 is None and manifest.signature == ""


def test_build_refuses_corpus_that_does_not_match_findings(tmp_path: Path) -> None:
    runs, _, _ = _scenario_runs(tmp_path)
    other = tmp_path / "corpus"
    other.mkdir()
    (other / "x.yaml").write_text(
        (ROOT / "scenarios" / "benign-order-status.yaml")
        .read_text()
        .replace("benign-order-status", "x")
    )
    with pytest.raises(PackError, match="does not match"):
        build(runs, tmp_path / "c.zip", corpus=other)
    with Recorder.start(tmp_path / "plain", agent_id="bot", run_id="r") as rec:
        rec.outcome("done", success=True)
    with pytest.raises(PackError, match="findings.json"):
        build(tmp_path / "plain", tmp_path / "d.zip", corpus=ROOT / "scenarios")
```

**Step 3:** run, expect `ModuleNotFoundError: No module named 'seatbelt.report.pack'`.

**Step 4: implement** `src/seatbelt/report/pack.py` (build half; `verify_pack` comes in Task 2):

```python
"""Evidence pack: one zip of ledgers, attestations and findings, bound by a signed manifest."""

from __future__ import annotations

import hashlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from seatbelt import __version__
from seatbelt.attest.manifest import Signed, sidecar
from seatbelt.attest.sign import Signer
from seatbelt.scenarios.model import corpus_sha256
from seatbelt.scenarios.runner import Report
from seatbelt.verify.chain import verify_file

PACK_VERSION = 1
MANIFEST = "pack.json"
_EPOCH = (
    1980,
    1,
    1,
    0,
    0,
    0,
)  # zip's earliest timestamp; fixed so identical inputs give identical members


class PackError(Exception):
    """The pack cannot be built. The message names the file."""


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    bytes: int


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
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
        blobs.append((f"runs/{ledger.name}", _read(ledger)))
        side = sidecar(ledger)
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
    return manifest


def _add(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)
```

`tests/unit/test_pack.py` imports `examples.scenario_target`, which works because `pythonpath = ["."]` is set for pytest.

**Step 5:** `uv run pytest -q`, ruff, pyright clean.

---

### Task 2: `verify_pack`

**Files:**
- Modify: `src/seatbelt/report/pack.py`
- Test: `tests/unit/test_pack.py`

**Step 1: failing tests** (append; add imports `from collections.abc import Callable`, `from seatbelt.report.pack import PackStatus, verify_pack`, `from seatbelt.verify.attest import Attestation`, `from seatbelt.attest.manifest import AttestError`)

```python
def _rezip(src: Path, dst: Path, edit: Callable[[dict[str, bytes]], None]) -> Path:
    members = _members(src)
    edit(members)
    with zipfile.ZipFile(dst, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return dst


def _pack(tmp_path: Path) -> tuple[Path, Path]:
    runs, key, pub = _scenario_runs(tmp_path)
    out = tmp_path / "audit.seatbelt.zip"
    build(runs, out, signer=Signer.from_file(key), corpus=ROOT / "scenarios")
    return out, pub


def test_clean_pack_is_attested_and_every_ledger_ok(tmp_path: Path) -> None:
    out, pub = _pack(tmp_path)
    verdict = verify_pack(out, pub)
    assert verdict.status is PackStatus.ATTESTED and verdict.ok, verdict.reason
    assert len(verdict.ledgers) == 8
    assert all(s.chain == "ok" and s.attestation is Attestation.ATTESTED for s in verdict.ledgers)


def test_unsigned_and_unchecked_packs(tmp_path: Path) -> None:
    runs, _, pub = _scenario_runs(tmp_path)
    out = build(runs, tmp_path / "u.zip") and tmp_path / "u.zip"
    assert verify_pack(out, pub).status is PackStatus.UNSIGNED
    signed, _ = _pack(tmp_path / "s")
    assert verify_pack(signed, None).status is PackStatus.UNCHECKED
    assert all(s.attestation is Attestation.UNCHECKED for s in verify_pack(signed, None).ledgers)


@pytest.mark.parametrize(
    ("name", "edit", "reason"),
    [
        (
            "edit-ledger",
            lambda m: m.update(
                {"runs/benign-order-status.jsonl": m["runs/benign-order-status.jsonl"] + b"\n"}
            ),
            "benign-order-status.jsonl",
        ),
        ("drop-member", lambda m: m.pop("runs/benign-order-status.attest.json"), "missing"),
        ("stray-member", lambda m: m.update({"runs/extra.txt": b"x"}), "not in manifest"),
        (
            "edit-manifest",
            lambda m: m.update({MANIFEST: m[MANIFEST].replace(b'"findings": 1', b'"findings": 0')}),
            "signature",
        ),
        (
            "swap-sidecar",
            lambda m: m.update(
                {"runs/benign-order-status.attest.json": m["runs/poisoned-context.attest.json"]}
            ),
            "benign-order-status.attest.json",
        ),
        ("no-manifest", lambda m: m.pop(MANIFEST), MANIFEST),
        ("garbage-manifest", lambda m: m.update({MANIFEST: b"{nope"}), "not a pack manifest"),
        ("zip-slip", lambda m: m.update({"../escape.txt": b"x"}), "unsafe"),
    ],
)
def test_tampered_pack_is_forged_naming_the_member(
    tmp_path: Path, name: str, edit: Callable[[dict[str, bytes]], None], reason: str
) -> None:
    out, pub = _pack(tmp_path)
    bad = _rezip(out, tmp_path / f"{name}.zip", edit)
    verdict = verify_pack(bad, pub)
    assert verdict.status is PackStatus.FORGED and not verdict.ok
    assert reason in (verdict.reason or ""), verdict.reason
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path.parent / "escape.txt").exists()


def test_wrong_key_bad_key_and_not_a_zip(tmp_path: Path) -> None:
    out, _ = _pack(tmp_path)
    _, other = keygen(tmp_path / "other")
    assert verify_pack(out, other).status is PackStatus.FORGED
    (tmp_path / "bad.pub").write_text("nope")
    with pytest.raises(AttestError):
        verify_pack(out, tmp_path / "bad.pub")
    (tmp_path / "not.zip").write_text("hello")
    assert verify_pack(tmp_path / "not.zip", None).status is PackStatus.FORGED


def test_dangling_evidence_and_manifest_findings_mismatch_are_forged(tmp_path: Path) -> None:
    runs, key, pub = _scenario_runs(tmp_path)
    report = runs / "findings.json"
    report.write_text(
        report.read_text().replace('"evidence": [\n      "', '"evidence": [\n      "ffffffff')
    )
    out = tmp_path / "dangling.zip"
    build(runs, out, signer=Signer.from_file(key))
    verdict = verify_pack(out, pub)
    assert verdict.status is PackStatus.FORGED and "evidence" in (verdict.reason or "")


def test_broken_ledger_inside_a_signed_pack_is_reported_not_hidden(tmp_path: Path) -> None:
    runs, key, pub = _scenario_runs(tmp_path)
    out = tmp_path / "ok.zip"
    build(runs, out, signer=Signer.from_file(key))
    # simulate a pack built by a tool that skipped the chain check: re-sign a manifest over a broken member
    from seatbelt.report.pack import build as _build  # noqa: F401  (documents intent)

    members = _members(out)
    manifest = PackManifest.model_validate_json(members[MANIFEST])
    broken = members["runs/benign-order-status.jsonl"].replace(b"delivered", b"lost")
    members["runs/benign-order-status.jsonl"] = broken
    for m in manifest.members:
        if m.path == "runs/benign-order-status.jsonl":
            m.sha256, m.bytes = hashlib.sha256(broken).hexdigest(), len(broken)
    manifest = Signer.from_file(key).sign(manifest.model_copy(update={"signature": ""}))
    members[MANIFEST] = manifest.model_dump_json(indent=2).encode() + b"\n"
    with zipfile.ZipFile(tmp_path / "broken.zip", "w") as zf:
        for n, d in members.items():
            zf.writestr(n, d)
    verdict = verify_pack(tmp_path / "broken.zip", pub)
    assert verdict.status is PackStatus.ATTESTED and not verdict.ok
    assert any(s.chain == "broken" and s.run_id == "benign-order-status" for s in verdict.ledgers)
```

Drop the `from seatbelt.report.pack import build as _build` line in the last test; it is noise. In the `edit-manifest` case the reason is "signature" because editing a signed field breaks the signature first. In `swap-sidecar` the member hash check fires (the swapped bytes differ from the manifest), so the reason names the sidecar path.

**Step 2:** run, expect `ImportError: cannot import name 'verify_pack'`.

**Step 3: implement** (append to `pack.py`; add imports `import tempfile`, `from dataclasses import dataclass, field`, `from enum import StrEnum`, `from pathlib import PurePosixPath`, `from seatbelt.attest.sign import load_public_key`, `from seatbelt.ledger.store import LedgerError, read_events`, `from seatbelt.verify.attest import Attestation, verify_attestation, verify_signature`):

```python
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
    ledgers: list[LedgerStatus] = field(default_factory=list)
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not PackStatus.FORGED and all(
            s.chain != "broken" for s in self.ledgers
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


def verify_pack(path: Path, pubkey: Path | None = None) -> PackVerdict:
    key = load_public_key(pubkey) if pubkey is not None else None  # a typo fails even unsigned
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        return _forged(f"{path}: not a zip: {exc}")
    with zf:
        names = zf.namelist()
        unsafe = [n for n in names if not _safe(n)]
        if unsafe:
            return _forged(f"unsafe member path {unsafe[0]!r}")
        if MANIFEST not in names:
            return _forged(f"no {MANIFEST} in the pack")
        try:
            manifest = PackManifest.model_validate_json(zf.read(MANIFEST))
        except ValidationError as exc:
            return _forged(f"{MANIFEST} is not a pack manifest: {exc}")
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
        expected = {m.path for m in manifest.members}
        actual = set(names) - {MANIFEST}
        if stray := sorted(actual - expected):
            return _forged(f"members not in manifest: {stray}")
        if missing := sorted(expected - actual):
            return _forged(f"members missing from the zip: {missing}")
        for m in manifest.members:
            data = zf.read(m.path)
            if len(data) != m.bytes or hashlib.sha256(data).hexdigest() != m.sha256:
                return _forged(f"{m.path}: content does not match the manifest")
        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)  # every name passed _safe above
            return _verify_contents(Path(tmp), manifest, pubkey, status)


def _verify_contents(
    root: Path, manifest: PackManifest, pubkey: Path | None, status: PackStatus
) -> PackVerdict:
    listed = {r.run_id for r in manifest.runs}
    for m in manifest.members:
        if m.path.startswith("runs/") and m.path.endswith(".jsonl") and m.path[5:-6] not in listed:
            return _forged(f"{m.path} is not listed under runs")
    ledgers: list[LedgerStatus] = []
    for run in manifest.runs:
        ledger = root / "runs" / f"{run.run_id}.jsonl"
        if not ledger.exists():
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
        for f in report.findings:
            ledger = root / "runs" / f"{f.scenario_id}.jsonl"
            try:
                known = {e.id for e in read_events(ledger)} if ledger.exists() else set[str]()
            except LedgerError:
                known = set()
            if not set(f.evidence) <= known:
                return _forged(
                    f"finding {f.scenario_id} / {f.check}: evidence is not in its ledger"
                )
    elif manifest.findings is not None:
        return _forged("manifest counts findings but findings.json is absent")
    corpus = root / "corpus"
    if (
        corpus.exists()
        and manifest.corpus_sha256
        and corpus_sha256(corpus) != manifest.corpus_sha256
    ):
        return _forged("corpus does not match corpus_sha256")
    return PackVerdict(status, ledgers)
```

`zipfile` never creates symlinks on extraction, so a symlink entry lands as a regular file whose content is the link target; `_safe` covers traversal.

**Step 4:** `uv run pytest -q`, ruff, pyright clean. If pyright objects to `set[str]()`, use `set()` with an annotation on `known`.

---

### Task 3: CLI, schema export, walkthrough

**Files:**
- Modify: `src/seatbelt/cli.py`, `scripts/export_schemas.py`, `scripts/walkthrough.sh`
- Create: `docs/schema/pack.json` (generated)
- Test: `tests/unit/test_pack.py`

**Step 1: failing tests** (append; imports `from typer.testing import CliRunner`, `from seatbelt.cli import app`; `runner = CliRunner()`)

```python
def test_cli_pack_and_verify_pack(tmp_path: Path) -> None:
    runs, key, pub = _scenario_runs(tmp_path)
    out = tmp_path / "audit.seatbelt.zip"
    made = runner.invoke(
        app,
        [
            "pack",
            str(runs),
            "--out",
            str(out),
            "--key",
            str(key),
            "--corpus",
            str(ROOT / "scenarios"),
        ],
    )
    assert made.exit_code == 0, made.output
    assert "wrote" in made.output
    assert runner.invoke(app, ["pack", str(runs), "--out", str(out)]).exit_code == 1
    good = runner.invoke(app, ["verify-pack", str(out), "--pubkey", str(pub)])
    assert good.exit_code == 0, good.output
    assert "attested" in good.output and "benign-order-status" in good.output
    unchecked = runner.invoke(app, ["verify-pack", str(out)])
    assert unchecked.exit_code == 0 and "UNCHECKED" in unchecked.output
    bad = _rezip(out, tmp_path / "bad.zip", lambda m: m.pop("findings.json"))
    forged = runner.invoke(app, ["verify-pack", str(bad), "--pubkey", str(pub)])
    assert forged.exit_code == 1 and "FORGED" in forged.output
    assert runner.invoke(app, ["verify-pack", str(tmp_path / "nope.zip")]).exit_code == 1


def test_pack_schema_is_exported() -> None:
    from seatbelt.report.pack import PackManifest as Model

    assert (
        json.loads((ROOT / "docs" / "schema" / "pack.json").read_text())
        == Model.model_json_schema()
    )
```

**Step 2:** run, expect exit code 2 "No such command 'pack'".

**Step 3: implement.** In `cli.py` add imports:

```python
from seatbelt.report.pack import PackError, PackStatus
from seatbelt.report.pack import build as build_pack
from seatbelt.report.pack import verify_pack as check_pack
```

and commands (after `attest`):

```python
KeyOpt = Annotated[Path | None, typer.Option(help="private key from keygen; signs the output")]


@app.command()
def pack(
    runs_dir: Path,
    out: Annotated[Path, typer.Option(help="evidence pack to write, e.g. audit.seatbelt.zip")],
    key: KeyOpt = None,
    corpus: Annotated[
        Path | None, typer.Option(help="scenario corpus to include; must match findings.json")
    ] = None,
) -> None:
    """Bundle a runs directory into an evidence pack. Refuses a broken ledger."""
    try:
        signer = Signer.from_file(key) if key else None
        manifest = build_pack(runs_dir, out, signer=signer, corpus=corpus)
    except (AttestError, PackError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"wrote {escape(str(out))}: {len(manifest.runs)} runs, {len(manifest.members)} members"
    )


@app.command(name="verify-pack")
def verify_pack_command(pack: Path, pubkey: PubKey = None) -> None:
    """Check an evidence pack offline: manifest, signature, members, chains, attestations."""
    try:
        verdict = check_pack(pack, pubkey)
    except AttestError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if verdict.status is PackStatus.FORGED:
        console.print(f"[red]FORGED[/]: {escape(verdict.reason or '')}")
        raise typer.Exit(code=1)
    table = Table("run", "chain", "attestation")
    for s in verdict.ledgers:
        table.add_row(escape(s.run_id), s.chain, s.attestation)
    console.print(table)
    if verdict.status is not PackStatus.ATTESTED:
        console.print(f"[yellow]{verdict.status.upper()}[/] pack signature not checked")
    if not verdict.ok:
        console.print("[red]BROKEN[/] a ledger in this pack fails its chain check")
        raise typer.Exit(code=1)
    console.print(f"[green]ok[/] {len(verdict.ledgers)} runs, pack {verdict.status}")
```

The existing `attest` command uses a `key` option typed inline; leave it, `KeyOpt` is for the new command (reuse it there only if the change is trivial).

`scripts/export_schemas.py`: add `from seatbelt.report.pack import PackManifest` and `"pack.json": PackManifest` to `SCHEMAS`; run it. The existing `test_checked_in_schemas_are_current` in `test_scenarios.py` then covers it too; keep `test_pack_schema_is_exported` anyway as the local guard.

`scripts/walkthrough.sh`: after step 7, before `step "Result"`:

```bash
step "8. Evidence pack (one signed zip; verify-pack re-checks every member offline)"
show "seatbelt pack runs --out audit.seatbelt.zip --key keys/seatbelt.key"
sb pack runs --out audit.seatbelt.zip --key keys/seatbelt.key
show "seatbelt verify-pack audit.seatbelt.zip --pubkey keys/seatbelt.pub"
sb verify-pack audit.seatbelt.zip --pubkey keys/seatbelt.pub
uv run --quiet --project "$REPO" python - audit.seatbelt.zip tamper/pack.zip <<'EOF'
import sys, zipfile
src, dst = sys.argv[1:]
with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst, "w") as b:
    for n in a.namelist():
        if not n.endswith(".attest.json"):  # drop every attestation sidecar
            b.writestr(n, a.read(n))
EOF
show "seatbelt verify-pack tamper/pack.zip --pubkey keys/seatbelt.pub   (sidecars removed)"
if sb verify-pack tamper/pack.zip --pubkey keys/seatbelt.pub | head -1; then echo "  pack tamper: NOT DETECTED"; FAILED=1; fi
```

`bash -n scripts/walkthrough.sh` must pass; do not run the script.

**Step 4:** `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run pyright && uv run pre-commit run --all-files`, all clean.

---

### Task 4: Spec, ADR, changelog, roadmap, README

**Files:**
- Create: `docs/spec/evidence-pack-v1.md`, `docs/adr/0004-evidence-pack.md`
- Modify: `CHANGELOG.md`, `ROADMAP.md`, `README.md`

**Spec** `docs/spec/evidence-pack-v1.md`:

```markdown
# Evidence pack format, version 1

An evidence pack is one zip file that carries the ledgers of one or more agent runs, their attestation sidecars, the findings of a scenario run where there was one, and a manifest that binds them together. It is built by `seatbelt pack` and checked by `seatbelt verify-pack`. Nothing in it needs a network or a service to verify.

## Layout

| Entry | Required | Content |
|---|---|---|
| `pack.json` | yes | The manifest, always the last entry. |
| `runs/<run id>.jsonl` | one or more | A ledger exactly as written by the recorder. |
| `runs/<run id>.attest.json` | where present | The ledger's attestation sidecar (ADR 0002). |
| `findings.json` | when present in the source directory | The scenario report (`docs/schema/findings.json`). |
| `corpus/<scenario id>.yaml` | when `--corpus` was given | The scenario files whose hash `findings.json` names. |

Every entry has the zip timestamp 1980-01-01 00:00:00 and entries are written in sorted order, so two packs of the same inputs differ only in `pack.json` (`created_at` and, if signed, `signature`).

## Manifest fields

Schema: `docs/schema/pack.json`. Unknown fields are rejected.

| Field | Type | Meaning | Set by |
|---|---|---|---|
| `pack_version` | integer | Format version of this document; this spec is 1. A verifier rejects versions it does not know. | builder |
| `harness` | string | `seatbelt <version>` that built the pack. | builder |
| `created_at` | RFC 3339 timestamp | When the pack was built, by the builder's clock. Informational. | builder |
| `members` | list of `{path, sha256, bytes}` | Every entry in the zip except `pack.json`, its SHA-256 hex digest and its size in bytes. | builder |
| `runs` | list of `{run_id, events, complete, attested}` | One row per ledger: its event count, whether the chain ends in a matching `run.end`, and whether a sidecar is present. | builder |
| `findings` | integer or null | Number of findings in `findings.json`; null when there is no report. | builder |
| `corpus_sha256` | string or null | Corpus hash copied from `findings.json`; null when there is no report. | builder |
| `public_key` | base64 string | Raw Ed25519 public key of the signer. Informational: a verifier trusts only the key file it is given. Empty when unsigned. | builder |
| `signature` | base64 string | Ed25519 signature over the canonical form. Empty when unsigned. | builder |

Canonical form: JSON of every field except `signature`, keys sorted, no whitespace, UTF-8.

## Verification

`seatbelt verify-pack <zip> [--pubkey <file>]` performs these steps in order and stops at the first failure, which it reports as FORGED with the offending member named:

1. If a public key file was given, load it. A file that is not an Ed25519 public key is an error, even before the pack is opened.
2. Open the zip. Reject any entry whose path is absolute, contains `..`, or contains a backslash.
3. Read `pack.json`. Reject a missing or unparseable manifest, or one whose `pack_version` is unknown.
4. Signature: empty means UNSIGNED; present but no key given means UNCHECKED; otherwise verify with the given key, FORGED on mismatch.
5. The set of entries other than `pack.json` must equal the `members` paths: an extra or missing entry is FORGED.
6. Each member's size and SHA-256 must match the manifest.
7. Extract to a temporary directory. For every row in `runs`: the ledger must exist, its chain is verified (ADR 0001), its attestation is verified against the given key (ADR 0002) and a forged attestation is FORGED. For an intact chain the event count, completeness and presence of a sidecar must match the row. A ledger entry not listed in `runs` is FORGED.
8. If `findings.json` is present it must parse, its finding count and corpus hash must match the manifest, and every finding's evidence ids must exist in that scenario's ledger. If the manifest counts findings but the file is absent, FORGED.
9. If `corpus/` is present its hash must equal `corpus_sha256`.

## Verdicts

| Verdict | Meaning | Exit code |
|---|---|---|
| `attested` | Signature verified with the given key and every step passed. | 0 |
| `unsigned` | No signature in the manifest; members, chains and attestations still verified. | 0, with a warning |
| `unchecked` | Signed, but no public key was given; everything except the pack signature verified. | 0, with a warning |
| `forged` | A step failed. The reason names the member. | 1 |

Independently of the pack verdict, each ledger is reported as `ok`, `incomplete` (chain intact, no matching `run.end`) or `broken`. A broken ledger makes the command exit 1 even in an attested pack: the builder refuses to pack one, so its presence means the pack was produced by something else.

## Trust boundary

The pack key is the attestation key (ADR 0002). A verified pack proves the set has not changed since it was built by whoever held the key. It does not prove the runs were honest, and `created_at` is the builder's own clock.
```

**ADR 0004** `docs/adr/0004-evidence-pack.md`:

```markdown
# 4. Evidence pack: a zip bound by a signed manifest

- Status: accepted
- Date: 2026-09-17

## Context

Ledgers, attestation sidecars and a findings report reach a reviewer as loose files. Nothing binds the set: a file can be dropped or swapped, and nothing says which harness, corpus and key produced them. STANDARDS asks for a pack format documented as a versioned spec.

## Decision

One zip per pack with a signed `pack.json` manifest listing every member's SHA-256 and size, one summary row per run, the findings count and corpus hash, signed with the attestation key. `seatbelt pack` builds it from a runs directory; `seatbelt verify-pack` re-verifies the manifest, every member, every chain, every attestation and every finding's evidence offline. The format is `docs/spec/evidence-pack-v1.md`.

The builder refuses a broken ledger: a pack must never launder a bad record. Incomplete ledgers are packed and marked. Zip entries carry a fixed timestamp and sorted order so members are byte-identical across builds.

Rejected: a plain directory (nothing binds the set until it is zipped anyway), one inlined JSON document (duplicates every ledger and grows without bound), rendered PDF or HTML output (a later layer over the same pack).

## Consequences

- A reviewer gets one object, one command and one verdict, and can still open any ledger inside with `verify` and `reconstruct`.
- Verification re-runs the chain and attestation checks rather than trusting the manifest's summary rows, so the pack adds integrity for the set without weakening the per-ledger guarantees.
- The manifest's `pack_version` lets the format change without silent misreads; a change is a spec revision and a schema update.
- Extraction is to a temporary directory after path checks; a pack cannot write outside it.
- `created_at` comes from the builder's clock. A timestamp authority is out of scope.
```

**CHANGELOG** `[Unreleased]` → add to `### Added`:

```markdown
- Evidence pack (`seatbelt.report.pack`): `seatbelt pack <runs_dir> --out audit.seatbelt.zip [--key] [--corpus]` bundles ledgers, attestation sidecars, `findings.json` and the corpus into one zip with a signed `pack.json` manifest; `seatbelt verify-pack <zip> [--pubkey]` re-checks the signature, every member hash, every chain, every attestation and every finding's evidence offline. Format: `docs/spec/evidence-pack-v1.md`, ADR 0004, schema `docs/schema/pack.json`.
- `seatbelt.attest.manifest.Signed`, the shared base for signed documents; `Signer.sign` accepts any `Signed`.
```

**ROADMAP** 0.1.0: prefix the evidence-pack bullet with `Done:`.

**README**: after the "Attack it." block:

````markdown
**Hand it over.** One zip, one command to check it.

```sh
uv run seatbelt pack runs --out audit.seatbelt.zip --key keys/seatbelt.key --corpus scenarios/
uv run seatbelt verify-pack audit.seatbelt.zip --pubkey keys/seatbelt.pub
```

The pack carries every ledger, its attestation, the findings and the corpus, bound by a signed manifest. Format: [`docs/spec/evidence-pack-v1.md`](docs/spec/evidence-pack-v1.md).
````

**Verify:** `uv run pre-commit run --all-files && uv run pytest -q`. Then stop; the user commits.
