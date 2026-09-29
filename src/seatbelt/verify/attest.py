"""Attestation check: does the sidecar manifest, signed by a trusted key, describe this ledger?"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from seatbelt.attest.manifest import ATTEST_VERSION, AttestError, Manifest, Signed, build, sidecar
from seatbelt.attest.sign import load_public_key
from seatbelt.ledger.store import LedgerError


class Attestation(StrEnum):
    UNATTESTED = "unattested"  # no sidecar
    UNCHECKED = "unchecked"  # sidecar present and matching, no public key supplied
    ATTESTED = "attested"
    FORGED = "forged"


@dataclass(frozen=True)
class AttestVerdict:
    status: Attestation
    reason: str | None = None


_PINNED = ("run_id", "schema_version", "events", "final_hash", "ledger_sha256")


def verify_signature(key: Ed25519PublicKey, signed: Signed) -> bool:
    try:
        key.verify(base64.b64decode(signed.signature), signed.canonical())
    except (InvalidSignature, ValueError):
        return False
    return True


def verify_attestation(ledger: Path, pubkey: Path | None) -> AttestVerdict:
    key = load_public_key(pubkey) if pubkey is not None else None  # a typo fails even unattested
    return check_attestation(ledger, key)


def check_attestation(ledger: Path, key: Ed25519PublicKey | None) -> AttestVerdict:
    """Without a key the sidecar's fields are still compared with the ledger. A mismatch is
    FORGED, but it is only an inconsistency: whoever edits the ledger can edit those too."""
    side = sidecar(ledger)
    if not side.exists():
        return AttestVerdict(Attestation.UNATTESTED)
    forged = Attestation.FORGED
    unkeyed = " (not checked against a key)" if key is None else ""
    try:
        manifest = Manifest.model_validate_json(side.read_bytes())
    except (ValidationError, OSError) as exc:
        return AttestVerdict(forged, f"{side} is not a manifest{unkeyed}: {exc}")
    if manifest.attest_version != ATTEST_VERSION:
        return AttestVerdict(forged, f"unsupported attest_version {manifest.attest_version}")
    if key is not None and not verify_signature(key, manifest):
        return AttestVerdict(forged, "signature does not verify with the given key")
    try:
        actual = build(ledger)
    except (OSError, UnicodeDecodeError, LedgerError, AttestError) as exc:
        return AttestVerdict(forged, str(exc))
    for field in _PINNED:
        if getattr(manifest, field) != getattr(actual, field):
            if key is None:
                reason = f"signature file {side} does not match this ledger ({field}){unkeyed}"
                return AttestVerdict(forged, reason)
            return AttestVerdict(forged, f"{field} does not match the ledger")
    if key is None:
        return AttestVerdict(Attestation.UNCHECKED, f"{side} present; pass --pubkey to check it")
    return AttestVerdict(Attestation.ATTESTED)
