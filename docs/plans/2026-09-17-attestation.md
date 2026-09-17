# Attestation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **No git commits or pushes.** The user commits manually. Each task ends with a green test run, not a commit.

**Goal:** A signed Ed25519 manifest per run so truncating or rewriting the ledger tail is detectable, per `docs/plans/2026-09-17-attestation-design.md`.

**Architecture:** `seatbelt.attest` builds a `Manifest` over a finished ledger (final hash, event count, file digest) and signs it into a sidecar `<run_id>.attest.json`. `seatbelt.verify.attest` checks the sidecar against a trusted public key and the ledger. `Recorder.start(signer=...)` signs at run end; `seatbelt keygen`, `seatbelt attest`, and `seatbelt verify --pubkey` expose it.

**Tech Stack:** Python 3.12, Pydantic, Typer, `cryptography` (Ed25519, PEM). Tests with pytest and Typer `CliRunner`, keys generated in `tmp_path`.

Commands: `uv run pytest tests/unit/test_attest.py -v`, `uv run ruff check . && uv run ruff format .`, `uv run pyright`.

---

### Task 1: Dependency and manifest model

**Files:**
- Modify: `pyproject.toml` (via `uv add`)
- Create: `src/seatbelt/attest/__init__.py` (empty)
- Create: `src/seatbelt/attest/manifest.py`
- Create: `tests/unit/test_attest.py`

**Step 1: Add the dependency**

Run: `uv add cryptography`
Expected: `pyproject.toml` gains `"cryptography>=50.0.1"` under `dependencies`, `uv.lock` updated.

**Step 2: Write the failing tests**

```python
import base64
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seatbelt.attest.manifest import Manifest, build, sidecar
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
from seatbelt.record.recorder import Recorder

runner = CliRunner()


def _ledger(tmp_path: Path) -> Path:
    with Recorder.start(tmp_path, agent_id="bot", run_id="r") as rec:
        rec.outcome("done", success=True)
    return tmp_path / "r.jsonl"


def test_build_describes_the_finished_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    events = list(read_events(ledger))
    m = build(ledger)
    assert (m.run_id, m.events, m.final_hash) == ("r", 3, events[-1].hash)
    assert m.schema_version == events[-1].schema_version
    assert len(m.ledger_sha256) == 64
    assert m.signature == "" and m.public_key == ""


def test_sidecar_path_sits_beside_the_ledger() -> None:
    assert sidecar(Path("runs/abc.jsonl")) == Path("runs/abc.attest.json")


def test_canonical_excludes_signature_only() -> None:
    a = build_stub()
    b = a.model_copy(update={"signature": "x"})
    assert a.canonical() == b.canonical()
    assert a.model_copy(update={"events": 99}).canonical() != a.canonical()


def build_stub() -> Manifest:
    return Manifest(
        attest_version=1,
        run_id="r",
        schema_version=1,
        events=3,
        final_hash="f" * 64,
        ledger_sha256="d" * 64,
        harness="seatbelt test",
    )
```

**Step 3: Run to verify it fails**

Run: `uv run pytest tests/unit/test_attest.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'seatbelt.attest'`

**Step 4: Implement**

`src/seatbelt/attest/__init__.py`: empty file.

`src/seatbelt/attest/manifest.py`:

```python
"""Manifest over a finished ledger: the facts a signature pins that the writer cannot rewrite."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from seatbelt import __version__
from seatbelt.ledger.store import read_events

ATTEST_VERSION = 1


class AttestError(Exception):
    """Raised when a key or manifest cannot be used."""


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attest_version: int
    run_id: str
    schema_version: int
    events: int
    final_hash: str
    ledger_sha256: str
    harness: str
    signed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    public_key: str = ""  # informational; trust comes from the verifier's own key file
    signature: str = ""

    def canonical(self) -> bytes:
        data = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def sidecar(ledger: Path) -> Path:
    return ledger.with_suffix(".attest.json")


def build(ledger: Path) -> Manifest:
    events = list(read_events(ledger))
    if not events:
        raise AttestError(f"{ledger} has no events")
    last = events[-1]
    return Manifest(
        attest_version=ATTEST_VERSION,
        run_id=last.run_id,
        schema_version=last.schema_version,
        events=len(events),
        final_hash=last.hash,
        ledger_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest(),
        harness=f"seatbelt {__version__}",
    )
```

**Step 5: Run to verify it passes**

Run: `uv run pytest tests/unit/test_attest.py -v`
Expected: 3 PASS.

---

### Task 2: Signer, keygen, attest

**Files:**
- Create: `src/seatbelt/attest/sign.py`
- Test: `tests/unit/test_attest.py`

**Step 1: Write the failing tests** (append)

```python
from seatbelt.attest.manifest import AttestError
from seatbelt.attest.sign import KEY_FILE, PUB_FILE, Signer, attest, keygen, load_public_key


def test_keygen_writes_private_key_0600_and_refuses_overwrite(tmp_path: Path) -> None:
    key, pub = keygen(tmp_path / "keys")
    assert key.name == KEY_FILE and pub.name == PUB_FILE
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert b"PRIVATE KEY" in key.read_bytes() and b"PUBLIC KEY" in pub.read_bytes()
    with pytest.raises(AttestError, match="refusing to overwrite"):
        keygen(tmp_path / "keys")


def test_signature_verifies_with_the_public_key_file(tmp_path: Path) -> None:
    key, pub = keygen(tmp_path)
    signed = Signer.from_file(key).sign(build_stub())
    load_public_key(pub).verify(base64.b64decode(signed.signature), signed.canonical())
    assert base64.b64decode(signed.public_key) == load_public_key(pub).public_bytes_raw()


def test_from_file_rejects_a_non_ed25519_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "rsa.key"
    pem.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(AttestError, match="Ed25519"):
        Signer.from_file(pem)
    (tmp_path / "junk.key").write_text("not pem")
    with pytest.raises(AttestError, match="PEM"):
        Signer.from_file(tmp_path / "junk.key")


def test_attest_writes_a_private_sidecar_and_refuses_a_second(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    key, _ = keygen(tmp_path / "keys")
    out = attest(ledger, Signer.from_file(key))
    assert out == sidecar(ledger)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    m = Manifest.model_validate_json(out.read_bytes())
    assert m.events == 3 and m.signature
    with pytest.raises(AttestError, match="exists"):
        attest(ledger, Signer.from_file(key))
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_attest.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'seatbelt.attest.sign'`

**Step 3: Implement**

`src/seatbelt/attest/sign.py`:

```python
"""Ed25519 signing of manifests. Keys are PEM files so openssl can read them too."""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from seatbelt.attest.manifest import AttestError, Manifest, build, sidecar

KEY_FILE = "seatbelt.key"
PUB_FILE = "seatbelt.pub"
_PEM = serialization.Encoding.PEM


class Signer:
    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key

    @classmethod
    def generate(cls) -> Signer:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_file(cls, path: Path) -> Signer:
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            raise AttestError(f"{path}: not a PEM private key: {exc}") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise AttestError(f"{path}: not an Ed25519 key")
        return cls(key)

    def sign(self, manifest: Manifest) -> Manifest:
        public = base64.b64encode(self._key.public_key().public_bytes_raw()).decode()
        unsigned = manifest.model_copy(update={"public_key": public})
        signature = base64.b64encode(self._key.sign(unsigned.canonical())).decode()
        return unsigned.model_copy(update={"signature": signature})

    def private_pem(self) -> bytes:
        return self._key.private_bytes(
            _PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )

    def public_pem(self) -> bytes:
        return self._key.public_key().public_bytes(
            _PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


def load_public_key(path: Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise AttestError(f"{path}: not a PEM public key: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AttestError(f"{path}: not an Ed25519 key")
    return key


def keygen(directory: Path) -> tuple[Path, Path]:
    key, pub = directory / KEY_FILE, directory / PUB_FILE
    for path in (key, pub):
        if path.exists():
            raise AttestError(f"{path} exists; refusing to overwrite")
    directory.mkdir(parents=True, exist_ok=True)
    signer = Signer.generate()
    key.touch(mode=0o600)
    key.write_bytes(signer.private_pem())
    pub.write_bytes(signer.public_pem())
    return key, pub


def attest(ledger: Path, signer: Signer) -> Path:
    out = sidecar(ledger)
    if out.exists():
        raise AttestError(f"{out} exists; delete it to re-sign")
    manifest = signer.sign(build(ledger))
    out.touch(mode=0o600)
    out.write_text(manifest.model_dump_json() + "\n", encoding="utf-8")
    return out
```

**Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_attest.py -v`
Expected: 7 PASS. Then `uv run pyright` clean (cryptography ships types; `isinstance` narrows the union).

---

### Task 3: Verification

**Files:**
- Create: `src/seatbelt/verify/attest.py`
- Test: `tests/unit/test_attest.py`

**Step 1: Write the failing tests** (append)

```python
from seatbelt.verify.attest import Attestation, verify_attestation
from seatbelt.verify.chain import verify_file


def _attested(tmp_path: Path) -> tuple[Path, Path]:
    ledger = _ledger(tmp_path)
    key, pub = keygen(tmp_path / "keys")
    attest(ledger, Signer.from_file(key))
    return ledger, pub


def test_untouched_ledger_is_attested(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    assert verify_attestation(ledger, pub).status is Attestation.ATTESTED


def test_no_sidecar_is_unattested_and_no_pubkey_is_unchecked(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert verify_attestation(ledger, None).status is Attestation.UNATTESTED
    attest(ledger, Signer.generate())
    assert verify_attestation(ledger, None).status is Attestation.UNCHECKED


def test_forged_tail_passes_the_chain_but_not_attestation(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-2]) + "\n")  # drop outcome and run.end
    Ledger(ledger, "r").append(
        Kind.RUN_END,
        Actor(type=ActorType.AGENT, id="bot"),
        {"run.ok": True, "run.error": None, "run.events": 2},
    )
    chain = verify_file(ledger)
    assert chain.ok and chain.complete  # the ADR 0001 gap
    verdict = verify_attestation(ledger, pub)
    assert verdict.status is Attestation.FORGED
    assert "events" in (verdict.reason or "")


def test_any_byte_change_is_forged(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    with ledger.open("a") as fh:
        fh.write("\n")  # blank line: chain still verifies, digest does not
    assert verify_file(ledger).ok
    assert verify_attestation(ledger, pub).status is Attestation.FORGED


def test_truncated_ledger_is_forged(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-1]) + "\n")
    assert verify_attestation(ledger, pub).status is Attestation.FORGED


def test_wrong_key_edited_or_garbage_sidecar_is_forged(tmp_path: Path) -> None:
    ledger, _ = _attested(tmp_path)
    _, other = keygen(tmp_path / "other")
    assert verify_attestation(ledger, other).status is Attestation.FORGED
    side = sidecar(ledger)
    _, pub = (tmp_path / "keys" / KEY_FILE, tmp_path / "keys" / PUB_FILE)
    side.write_text(side.read_text().replace('"events":3', '"events":2'))
    assert verify_attestation(ledger, pub).status is Attestation.FORGED
    side.write_text("{not json")
    assert verify_attestation(ledger, pub).status is Attestation.FORGED


def test_bad_pubkey_file_raises(tmp_path: Path) -> None:
    ledger, _ = _attested(tmp_path)
    (tmp_path / "bad.pub").write_text("nope")
    with pytest.raises(AttestError, match="PEM"):
        verify_attestation(ledger, tmp_path / "bad.pub")
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_attest.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'seatbelt.verify.attest'`

**Step 3: Implement**

`src/seatbelt/verify/attest.py`:

```python
"""Attestation check: does the sidecar manifest, signed by a trusted key, describe this ledger?"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from pydantic import ValidationError

from seatbelt.attest.manifest import AttestError, Manifest, build, sidecar
from seatbelt.attest.sign import load_public_key
from seatbelt.ledger.store import LedgerError


class Attestation(StrEnum):
    UNATTESTED = "unattested"  # no sidecar
    UNCHECKED = "unchecked"  # sidecar present, no public key supplied
    ATTESTED = "attested"
    FORGED = "forged"


@dataclass(frozen=True)
class AttestVerdict:
    status: Attestation
    reason: str | None = None


_PINNED = ("run_id", "schema_version", "events", "final_hash", "ledger_sha256")


def verify_attestation(ledger: Path, pubkey: Path | None) -> AttestVerdict:
    side = sidecar(ledger)
    if not side.exists():
        return AttestVerdict(Attestation.UNATTESTED)
    if pubkey is None:
        return AttestVerdict(Attestation.UNCHECKED, f"{side} present; pass --pubkey to check it")
    key = load_public_key(pubkey)
    forged = Attestation.FORGED
    try:
        manifest = Manifest.model_validate_json(side.read_bytes())
    except (ValidationError, OSError) as exc:
        return AttestVerdict(forged, f"{side} is not a manifest: {exc}")
    try:
        key.verify(base64.b64decode(manifest.signature), manifest.canonical())
    except (InvalidSignature, ValueError):
        return AttestVerdict(forged, "signature does not verify with the given key")
    try:
        actual = build(ledger)
    except (OSError, LedgerError, AttestError) as exc:
        return AttestVerdict(forged, str(exc))
    for field in _PINNED:
        if getattr(manifest, field) != getattr(actual, field):
            return AttestVerdict(forged, f"{field} does not match the ledger")
    return AttestVerdict(Attestation.ATTESTED)
```

**Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_attest.py -v`
Expected: 14 PASS.

---

### Task 4: Recorder signs at run end

**Files:**
- Modify: `src/seatbelt/record/recorder.py:46-83`
- Test: `tests/unit/test_attest.py`

**Step 1: Write the failing tests** (append)

```python
def test_recorder_signs_at_run_end_even_when_the_run_fails(tmp_path: Path) -> None:
    key, pub = keygen(tmp_path / "keys")
    signer = Signer.from_file(key)
    with Recorder.start(tmp_path, agent_id="bot", run_id="ok", signer=signer) as rec:
        rec.outcome("done", success=True)
    assert verify_attestation(tmp_path / "ok.jsonl", pub).status is Attestation.ATTESTED
    with pytest.raises(RuntimeError):
        with Recorder.start(tmp_path, agent_id="bot", run_id="bad", signer=signer):
            raise RuntimeError("boom")
    assert verify_attestation(tmp_path / "bad.jsonl", pub).status is Attestation.ATTESTED
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_attest.py -k recorder_signs -v`
Expected: FAIL, `TypeError: ... unexpected keyword argument 'signer'`

**Step 3: Implement**

In `recorder.py`, add the import and parameter, and sign in `finally` after `run.end`:

```python
from seatbelt.attest.sign import Signer, attest
```

(remove the `TYPE_CHECKING` block only if nothing else uses it; `Policy` still does, so keep it.)

```python
        policy: Policy | None = None,
        signer: Signer | None = None,
    ) -> Generator[Recorder]:
```

```python
        finally:
            rec._emit(
                Kind.RUN_END,
                agent,
                {"run.ok": error is None, "run.error": error, "run.events": ledger.length + 1},
            )
            if signer is not None:
                attest(path, signer)  # a signed failure is still evidence
```

**Step 4: Run to verify it passes**

Run: `uv run pytest -q`
Expected: all pass (prior suite plus 15 attest tests).

---

### Task 5: CLI

**Files:**
- Modify: `src/seatbelt/cli.py`
- Test: `tests/unit/test_attest.py`

**Step 1: Write the failing tests** (append)

```python
from seatbelt.cli import app


def test_cli_keygen_attest_verify_round_trip(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    assert runner.invoke(app, ["keygen", str(keys)]).exit_code == 0
    assert runner.invoke(app, ["keygen", str(keys)]).exit_code == 1
    ledger = _ledger(tmp_path)
    key, pub = keys / KEY_FILE, keys / PUB_FILE
    plain = runner.invoke(app, ["verify", str(ledger)])
    assert plain.exit_code == 0 and "unattested" in plain.output
    signed = runner.invoke(app, ["attest", str(ledger), "--key", str(key)])
    assert signed.exit_code == 0, signed.output
    assert runner.invoke(app, ["attest", str(ledger), "--key", str(key)]).exit_code == 1
    unchecked = runner.invoke(app, ["verify", str(ledger)])
    assert unchecked.exit_code == 0 and "UNCHECKED" in unchecked.output
    good = runner.invoke(app, ["verify", str(ledger), "--pubkey", str(pub)])
    assert good.exit_code == 0 and "attested" in good.output
    assert runner.invoke(app, ["reconstruct", str(ledger), "--pubkey", str(pub)]).exit_code == 0


def test_cli_verify_reports_forged_and_attest_refuses_broken(tmp_path: Path) -> None:
    ledger, pub = _attested(tmp_path)
    with ledger.open("a") as fh:
        fh.write("\n")
    forged = runner.invoke(app, ["verify", str(ledger), "--pubkey", str(pub)])
    assert forged.exit_code == 1 and "FORGED" in forged.output
    assert runner.invoke(app, ["reconstruct", str(ledger), "--pubkey", str(pub)]).exit_code == 1
    ledger.write_text(ledger.read_text().replace("done", "undone"))
    sidecar(ledger).unlink()
    broken = runner.invoke(app, ["attest", str(ledger), "--key", str(tmp_path / "keys" / KEY_FILE)])
    assert broken.exit_code == 1 and "BROKEN" in broken.output
    assert not sidecar(ledger).exists()
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_attest.py -k cli -v`
Expected: FAIL, keygen exit code 2 (no such command).

**Step 3: Implement**

Replace `_check`, `verify`, `reconstruct` in `cli.py` and add `keygen`, `attest`:

```python
from typing import Annotated

from seatbelt.attest.manifest import AttestError
from seatbelt.attest.sign import Signer, attest as sign_ledger, keygen as make_keys
from seatbelt.verify.attest import Attestation, AttestVerdict, verify_attestation
```

```python
PubKey = Annotated[Path | None, typer.Option(help="public key from keygen; checks the attestation")]


def _check(ledger: Path, pubkey: Path | None = None) -> tuple[Verdict, AttestVerdict]:
    """Exit 1 on a broken chain or a forged attestation; warn on incomplete or unchecked."""
    verdict = verify_file(ledger)
    if not verdict.ok:
        where = "" if verdict.first_bad_seq is None else f" at seq {verdict.first_bad_seq}"
        console.print(f"[red]BROKEN[/]{where}: {escape(verdict.reason or '')}")
        raise typer.Exit(code=1)
    try:
        att = verify_attestation(ledger, pubkey)
    except AttestError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if att.status is Attestation.FORGED:
        console.print(f"[red]FORGED[/]: {escape(att.reason or '')}")
        raise typer.Exit(code=1)
    if att.status is Attestation.UNCHECKED:
        console.print(f"[yellow]UNCHECKED[/] {escape(att.reason or '')}")
    if not verdict.complete:
        console.print(
            f"[yellow]INCOMPLETE[/] {verdict.events} events, chain intact but no matching "
            "run.end: truncated or still running"
        )
    return verdict, att


@app.command()
def verify(ledger: Path, pubkey: PubKey = None) -> None:
    """Check a run ledger's hash chain and attestation. Exit 1 if altered, forged or incomplete."""
    verdict, att = _check(ledger, pubkey)
    if not verdict.complete:
        raise typer.Exit(code=1)
    console.print(f"[green]ok[/] {verdict.events} events, chain intact, {att.status}")


@app.command()
def reconstruct(ledger: Path, pubkey: PubKey = None) -> None:
    """Print the run as a timeline a reviewer can read. Refuses an altered ledger."""
    _check(ledger, pubkey)
    timeline(ledger, console)


@app.command()
def keygen(directory: Path = Path(".")) -> None:
    """Write an Ed25519 signing key (seatbelt.key, mode 0600) and its public key (seatbelt.pub)."""
    try:
        key, pub = make_keys(directory)
    except AttestError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"wrote {key} and {pub}")


@app.command()
def attest(
    ledger: Path, key: Annotated[Path, typer.Option(help="private key from keygen")]
) -> None:
    """Sign a finished ledger into <run id>.attest.json. Refuses a broken chain."""
    _check(ledger)
    try:
        out = sign_ledger(ledger, Signer.from_file(key))
    except AttestError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"wrote {out}")
```

**Step 4: Run to verify it passes**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run pyright`
Expected: all pass, no lint or type errors. `test_cli.py` still passes (output for unattested ledgers now ends `, unattested`).

---

### Task 6: Docs, changelog, walkthrough

**Files:**
- Create: `docs/adr/0002-signed-run-manifest.md`
- Modify: `docs/adr/0001-hash-chained-jsonl-ledger.md` (truncation bullet)
- Modify: `CHANGELOG.md` `[Unreleased]`, `ROADMAP.md` 0.0.4, `README.md`, `scripts/walkthrough.sh`

**Step 1: ADR 0002**

```markdown
# 2. Signed run manifest

- Status: accepted
- Date: 2026-09-17

## Context

ADR 0001 detects any change inside the chain but not a rewritten tail: drop the last events, append a fresh `run.end` with a matching count, and `verify` passes. The final hash has to be recorded somewhere the ledger writer cannot rewrite.

## Decision

A signed manifest per run, `<run id>.attest.json` beside the ledger, holding the final event hash, the event count, the schema version, the run id and the SHA-256 of the ledger file, signed with Ed25519. Keys are PEM files written by `seatbelt keygen`; `seatbelt verify --pubkey` checks the signature with the reviewer's own copy of the public key, never the one embedded in the manifest.

Rejected: HMAC (whoever can verify can forge), Sigstore keyless (network at sign time, heavy dependency; the right next step for cross-organisation trust), an inline `run.attest` event (a schema bump, and a rewriter drops it like any tail).

## Consequences

- A truncated or rewritten tail, and any byte change to the file, is reported as FORGED when the public key is supplied. Without it the sidecar is UNCHECKED; without a sidecar the ledger is UNATTESTED. Neither downgrades the chain check.
- The key lives with the process that writes the ledger, so attestation proves the record has not changed since run end by anyone without the key. It does not prove the process was honest.
- Signing happens in `Recorder.start`'s `finally`, after `run.end`, so a failed run is signed too. A signing failure propagates; the ledger is already complete on disk.
- A sidecar is never overwritten. Re-signing means deleting it first, which is visible.
- `cryptography` becomes a hard dependency.
```

**Step 2: ADR 0001** — replace the second consequences bullet's last clause: "...which is the job of signed attestation (planned for 0.0.4)." → "...which is the job of signed attestation (ADR 0002)."

**Step 3: CHANGELOG** under `## [Unreleased]`:

```markdown
### Added
- Attestation (`seatbelt.attest`): `Recorder.start(..., signer=Signer.from_file(key))` signs an Ed25519 manifest (final hash, event count, file digest) into `<run id>.attest.json` at run end, failed runs included. `seatbelt keygen` writes the key pair, `seatbelt attest <ledger> --key` signs after the fact, and `seatbelt verify --pubkey` reports ATTESTED, FORGED (exit 1), UNCHECKED (sidecar but no key) or unattested. A truncated or rewritten tail, which the chain check alone accepts, is now detected. See ADR 0002.
- `cryptography` is a dependency.
```

**Step 4: ROADMAP** — change `## 0.0.4` to `## 0.0.4 (in progress)` and leave the bullet. (The release step marks it released.)

**Step 5: README** — after the "Try it in five minutes" block add:

```markdown
**Prove the tail too.** The chain catches edits inside the file; a signed manifest catches a rewritten ending.

```sh
uv run seatbelt keygen keys                                  # seatbelt.key (private, 0600), seatbelt.pub
uv run seatbelt attest runs/<run id>.jsonl --key keys/seatbelt.key
uv run seatbelt verify runs/<run id>.jsonl --pubkey keys/seatbelt.pub   # ATTESTED, or FORGED
```

Or sign at run end: `Recorder.start(..., signer=Signer.from_file(Path("keys/seatbelt.key")))`.
```

And in "What it produces", append: "A signed manifest beside it pins the final hash, so a rewritten tail fails too."

**Step 6: walkthrough.sh** — after step 6 add:

```bash
step "7. Attest (signed manifest catches a forged tail the chain accepts)"
sb keygen keys
show "seatbelt attest $LEDGER --key keys/seatbelt.key"
sb attest "$LEDGER" --key keys/seatbelt.key
show "seatbelt verify $LEDGER --pubkey keys/seatbelt.pub"
sb verify "$LEDGER" --pubkey keys/seatbelt.pub
cp "$LEDGER" tamper/forged.jsonl
cp "${LEDGER%.jsonl}.attest.json" tamper/forged.attest.json
uv run --quiet --project "$REPO" python - tamper/forged.jsonl <<'EOF'
import sys
from pathlib import Path
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
p = Path(sys.argv[1]); lines = p.read_text().splitlines()
p.write_text("\n".join(lines[:-2]) + "\n")
n = sum(1 for _ in read_events(p)) + 1
Ledger(p, next(read_events(p)).run_id).append(Kind.RUN_END, Actor(type=ActorType.AGENT, id="x"), {"run.ok": True, "run.error": None, "run.events": n})
EOF
show "seatbelt verify tamper/forged.jsonl            (chain alone: passes)"
sb verify tamper/forged.jsonl || true
show "seatbelt verify tamper/forged.jsonl --pubkey keys/seatbelt.pub"
if sb verify tamper/forged.jsonl --pubkey keys/seatbelt.pub | head -1; then echo "  forged tail: NOT DETECTED"; FAILED=1; fi
```

**Step 7: Verify everything**

Run: `uv run pre-commit run --all-files && uv run pytest -q`
Expected: all hooks pass, all tests pass. `bash -n scripts/walkthrough.sh` parses.

**Step 8: Stop.** Report results; the user commits.
