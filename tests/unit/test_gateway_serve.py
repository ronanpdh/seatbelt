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


class _Killed(Exception):
    pass


def test_sessions_are_signed_when_uvicorn_re_raises_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvicorn raises the stop signal again once it has stopped. Under the default SIGTERM
    handler that would kill the process before the open sessions are closed and signed; a
    second signal during the close is ignored for the same reason."""
    import signal

    import httpx2
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from seatbelt.attest.manifest import sidecar
    from seatbelt.gateway import serve as serve_mod
    from seatbelt.gateway.config import add_principal
    from seatbelt.verify.chain import verify_file

    keygen(tmp_path / "keys")
    path = _config(tmp_path, "keys/seatbelt.key")
    key = add_principal(path, "alice@corp")
    cfg = load_config(path)

    def fake_run(app: Starlette, **_: object) -> None:
        app.state.transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json={}))
        with TestClient(app) as client:
            client.post("/v1/messages", json={"model": "m"}, headers={"x-api-key": key})
        signal.raise_signal(signal.SIGTERM)  # what uvicorn does after a SIGTERM stop

    close_all = serve_mod.Sessions.close_all

    def impatient(self: serve_mod.Sessions, timeout: float = 30.0) -> int:
        signal.raise_signal(signal.SIGTERM)  # a second stop signal while sessions close
        return close_all(self, timeout)

    monkeypatch.setattr(serve_mod.Sessions, "close_all", impatient)

    def killed(_signum: int, _frame: object) -> None:
        raise _Killed  # stands in for the default handler, which would end the test run

    monkeypatch.setattr(serve_mod.uvicorn, "run", fake_run)
    previous = signal.signal(signal.SIGTERM, killed)
    try:
        serve_mod.serve(path)
        assert signal.getsignal(signal.SIGTERM) is killed  # put back after the close
    finally:
        signal.signal(signal.SIGTERM, previous)
    (ledger,) = cfg.ledgers.glob("*.jsonl")
    assert verify_file(ledger).complete and sidecar(ledger).exists()
