import base64
from pathlib import Path

import pytest

from seatbelt.attest.manifest import AttestError
from seatbelt.attest.sign import Signer, keygen
from seatbelt.gateway.config import load_config
from seatbelt.gateway.serve import KEY_ENV, load_signer

UPSTREAMS = "ledgers: runs\nupstreams:\n  anthropic: {url: https://x, key_env: K}\n"


def _config(tmp_path: Path, signing_key: str | None) -> Path:
    p = tmp_path / "gateway.yaml"
    p.write_text((f"signing_key: {signing_key}\n" if signing_key else "") + UPSTREAMS)
    return p


def _same_key(a: Signer, b: Signer) -> bool:
    return a.public_pem() == b.public_pem()


def test_key_file_from_the_config(tmp_path: Path) -> None:
    key, _ = keygen(tmp_path / "keys")
    cfg = load_config(_config(tmp_path, "keys/seatbelt.key"))
    assert _same_key(load_signer(cfg, {}), Signer.from_file(key))


@pytest.mark.parametrize("encode", ["pem", "flattened", "base64"])
def test_key_from_the_environment(tmp_path: Path, encode: str) -> None:
    key, _ = keygen(tmp_path / "keys")
    pem = key.read_text()
    value = {
        "pem": pem,
        "flattened": pem.replace("\n", "\\n"),  # how some env UIs store a multi-line value
        "base64": base64.b64encode(pem.encode()).decode(),
    }[encode]
    cfg = load_config(_config(tmp_path, None))
    assert cfg.signing_key is None
    assert _same_key(load_signer(cfg, {KEY_ENV: value}), Signer.from_file(key))


def test_both_or_neither_or_garbage_is_refused(tmp_path: Path) -> None:
    keygen(tmp_path / "keys")
    with_file = load_config(_config(tmp_path, "keys/seatbelt.key"))
    without = load_config(_config(tmp_path, None))
    with pytest.raises(AttestError, match="set one"):
        load_signer(with_file, {KEY_ENV: "anything"})
    with pytest.raises(AttestError, match="no signing key"):
        load_signer(without, {})
    with pytest.raises(AttestError, match="neither a PEM key nor base64"):
        load_signer(without, {KEY_ENV: "not a key!"})
    with pytest.raises(AttestError, match="not a PEM private key"):
        load_signer(without, {KEY_ENV: base64.b64encode(b"hello").decode()})
