"""Ed25519 signing of manifests. Keys are PEM files so openssl can read them too."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from uuid import uuid4

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from seatbelt.attest.manifest import AttestError, Signed, build, sidecar
from seatbelt.ledger.store import fsync_dir

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
            data = path.read_bytes()
        except OSError as exc:
            raise AttestError(f"{path}: not a PEM private key: {exc}") from exc
        return cls.from_pem(data, str(path))

    @classmethod
    def from_pem(cls, data: bytes, source: str = "key") -> Signer:
        try:
            key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            raise AttestError(f"{source}: not a PEM private key: {exc}") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise AttestError(f"{source}: not an Ed25519 key")
        return cls(key)

    def sign[M: Signed](self, manifest: M) -> M:
        public = base64.b64encode(self._key.public_key().public_bytes_raw()).decode()
        unsigned = manifest.model_copy(update={"public_key": public})
        signature = base64.b64encode(self._key.sign(unsigned.canonical())).decode()
        return unsigned.model_copy(update={"signature": signature})

    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

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
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise AttestError(f"{path}: not a PEM public key: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AttestError(f"{path}: not an Ed25519 key")
    return key


def _create(path: Path, data: bytes) -> None:
    """Create `path` (mode 0600, never over an existing file) holding `data`, fsynced."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AttestError(f"{path} exists; refusing to overwrite") from exc
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _write_private(path: Path, data: bytes) -> None:
    """Write `path` whole or not at all: a sidecar cut short by a crash would read as FORGED.
    The data goes to a temp file first; a hard link to the final name keeps no-overwrite."""
    if path.exists():
        raise AttestError(f"{path} exists; refusing to overwrite")
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _create(tmp, data)
        try:
            os.link(tmp, path)
        except FileExistsError as exc:
            raise AttestError(f"{path} exists; refusing to overwrite") from exc
        except OSError:  # no hard links on this filesystem: create in place, as before
            _create(path, data)
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(path.parent)


def keygen(directory: Path) -> tuple[Path, Path]:
    key, pub = directory / KEY_FILE, directory / PUB_FILE
    for path in (key, pub):
        if path.exists():
            raise AttestError(f"{path} exists; refusing to overwrite")
    directory.mkdir(parents=True, exist_ok=True)
    signer = Signer.generate()
    _write_private(key, signer.private_pem())
    _write_private(pub, signer.public_pem())
    return key, pub


def attest(ledger: Path, signer: Signer) -> Path:
    out = sidecar(ledger)
    manifest = signer.sign(build(ledger))
    _write_private(out, (manifest.model_dump_json() + "\n").encode())
    return out
