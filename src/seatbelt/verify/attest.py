"""Attestation check: does the sidecar manifest, signed by a trusted key, describe this ledger?"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from pydantic import ValidationError

from seatbelt.attest.manifest import ATTEST_VERSION, AttestError, Manifest, build, sidecar
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
    key = load_public_key(pubkey) if pubkey is not None else None  # a typo fails even unattested
    side = sidecar(ledger)
    if not side.exists():
        return AttestVerdict(Attestation.UNATTESTED)
    if key is None:
        return AttestVerdict(Attestation.UNCHECKED, f"{side} present; pass --pubkey to check it")
    forged = Attestation.FORGED
    try:
        manifest = Manifest.model_validate_json(side.read_bytes())
    except (ValidationError, OSError) as exc:
        return AttestVerdict(forged, f"{side} is not a manifest: {exc}")
    if manifest.attest_version != ATTEST_VERSION:
        return AttestVerdict(forged, f"unsupported attest_version {manifest.attest_version}")
    try:
        key.verify(base64.b64decode(manifest.signature), manifest.canonical())
    except (InvalidSignature, ValueError):
        return AttestVerdict(forged, "signature does not verify with the given key")
    try:
        actual = build(ledger)
    except (OSError, UnicodeDecodeError, LedgerError, AttestError) as exc:
        return AttestVerdict(forged, str(exc))
    for field in _PINNED:
        if getattr(manifest, field) != getattr(actual, field):
            return AttestVerdict(forged, f"{field} does not match the ledger")
    return AttestVerdict(Attestation.ATTESTED)
