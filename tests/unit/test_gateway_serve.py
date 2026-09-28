import base64
from collections.abc import Callable
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


MANGLES: list[Callable[[str], str]] = [
    lambda v: f'"{v}"',  # quoted by the env var editor
    lambda v: f"'{v}'",
    lambda v: v[:40] + "\n" + v[40:80] + " " + v[80:],  # a paste that wrapped or split it
    lambda v: f"  {v}\n",
    lambda v: v + "%",  # zsh's no-newline marker, copied from the terminal
]


@pytest.mark.parametrize("mangle", MANGLES)
def test_key_from_the_environment_survives_what_editors_add(
    tmp_path: Path, mangle: Callable[[str], str]
) -> None:
    key, _ = keygen(tmp_path / "keys")
    value = mangle(base64.b64encode(key.read_bytes()).decode())
    cfg = load_config(_config(tmp_path, None))
    assert _same_key(load_signer(cfg, {KEY_ENV: value}), Signer.from_file(key))


def test_a_bad_env_key_is_described_without_revealing_it(tmp_path: Path) -> None:
    key, _ = keygen(tmp_path / "keys")
    good = base64.b64encode(key.read_bytes()).decode()
    cfg = load_config(_config(tmp_path, None))
    with pytest.raises(AttestError) as err:
        load_signer(cfg, {KEY_ENV: good[:40] + "A" + good[41:]})  # a character changed
    message = str(err.value)
    assert "not a PEM private key" in message and "starts like base64 of a PEM key" in message
    assert good[20:40] not in message  # no key material
    with pytest.raises(AttestError, match="cut short"):
        load_signer(cfg, {KEY_ENV: good[:-2]})
