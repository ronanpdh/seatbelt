import dataclasses
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.testclient import TestClient

from seatbelt.attest.sign import Signer
from seatbelt.gateway.app import Live, create_app
from seatbelt.gateway.config import OidcConfig, add_principal, load_config
from seatbelt.gateway.oidc import OidcError, OidcVerifier, looks_like_jwt
from seatbelt.gateway.sessions import Sessions
from seatbelt.ledger.store import read_events

ISSUER = "https://login.example.com/tenant/v2.0"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
JWKS_URL = "https://login.example.com/tenant/discovery/v2.0/keys"


class Provider:
    """An identity provider: a signing key, its published key set and a discovery document."""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.fetched: list[str] = []
        self.rotate("k1")

    def rotate(self, kid: str) -> None:
        self.kid = kid
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def fetch(self, url: str) -> dict[str, Any]:
        self.fetched.append(url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return {"issuer": ISSUER, "jwks_uri": JWKS_URL}
        assert url == JWKS_URL
        return {
            "keys": [
                {**RSAAlgorithm.to_jwk(k.public_key(), as_dict=True), "kid": kid, "alg": "RS256"}
                for kid, k in self.keys.items()
            ]
        }

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        body = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-1",
            "email": "alice@corp.example",
            "iat": now,
            "exp": now + 3600,
            **claims,
        }
        return jwt.encode(body, self.keys[self.kid], algorithm="RS256", headers={"kid": self.kid})


def _cfg(**extra: Any) -> OidcConfig:
    return OidcConfig(issuer=ISSUER, audience=CLIENT_ID, **extra)


@pytest.fixture
def idp() -> Provider:
    return Provider()


def test_a_valid_token_is_accepted_and_its_keys_fetched_once(idp: Provider) -> None:
    verifier = OidcVerifier(_cfg(), idp.fetch)
    assert verifier.verify(idp.token())["sub"] == "user-1"
    assert verifier.verify(idp.token(sub="user-2"))["sub"] == "user-2"
    assert idp.fetched == [f"{ISSUER}/.well-known/openid-configuration", JWKS_URL]


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"aud": "some-other-app"}, "(?i)audience"),  # another app in the same tenant
        ({"iss": "https://evil.example.com"}, "issuer"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"exp": None}, "exp"),
    ],
)
def test_audience_issuer_and_expiry_are_enforced(
    idp: Provider, claims: dict[str, Any], reason: str
) -> None:
    body = {k: v for k, v in claims.items() if v is not None}
    token = idp.token(**body)
    if claims.get("exp", 0) is None:  # a token with no expiry at all
        payload = jwt.decode(token, options={"verify_signature": False})
        payload.pop("exp")
        token = jwt.encode(payload, idp.keys["k1"], algorithm="RS256", headers={"kid": "k1"})
    with pytest.raises(OidcError, match=reason):
        OidcVerifier(_cfg(), idp.fetch).verify(token)


def _hand_signed(header: dict[str, Any], claims: dict[str, Any], secret: bytes | None) -> str:
    """A token PyJWT would refuse to produce: unsigned, or HMAC-keyed with a public key."""
    import base64
    import hashlib
    import hmac
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
    signature = hmac.new(secret, signing.encode(), hashlib.sha256).digest() if secret else b""
    return f"{signing}.{b64(signature)}"


def test_forged_tokens_are_refused(idp: Provider) -> None:
    verifier = OidcVerifier(_cfg(), idp.fetch)
    claims = jwt.decode(idp.token(), options={"verify_signature": False})
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        idp.keys["k1"]
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    forged = [
        jwt.encode(claims, stranger, algorithm="RS256", headers={"kid": "k1"}),  # wrong key
        _hand_signed({"alg": "none", "kid": "k1"}, claims, None),  # unsigned
        _hand_signed({"alg": "HS256", "kid": "k1"}, claims, public_pem),  # algorithm confusion
    ]
    for token in forged:
        with pytest.raises(OidcError):
            verifier.verify(token)


def test_a_rotated_key_is_fetched_but_not_more_than_once_per_cooldown(idp: Provider) -> None:
    now = [1000.0]
    verifier = OidcVerifier(_cfg(jwks_url=JWKS_URL), idp.fetch, clock=lambda: now[0])
    verifier.verify(idp.token())
    idp.rotate("k2")
    now[0] += 60  # past the cooldown, inside the lifespan
    verifier.verify(idp.token())  # names k2: fetched again
    assert idp.fetched == [JWKS_URL, JWKS_URL]
    now[0] += 1
    with pytest.raises(OidcError, match="no key 'k9'"):
        verifier.verify(
            jwt.encode({"a": 1}, idp.keys["k2"], algorithm="RS256", headers={"kid": "k9"})
        )
    assert len(idp.fetched) == 2  # within the cooldown: no fetch storm from junk key ids


def test_only_asymmetric_algorithms_are_configurable() -> None:
    with pytest.raises(ValueError, match="asymmetric"):
        _cfg(algorithms=["HS256"])
    with pytest.raises(ValueError, match="asymmetric"):
        _cfg(algorithms=[])
    assert looks_like_jwt(Provider().token()) and not looks_like_jwt("sbk_abc")


@dataclasses.dataclass
class Gw:
    client: TestClient
    key: str
    ledgers: Path
    idp: Provider

    def ask(self, token: str, path: str = "/v1/messages") -> httpx2.Response:
        body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
        return self.client.post(path, json=body, headers={"authorization": f"Bearer {token}"})

    def starts(self) -> list[dict[str, Any]]:
        return [next(read_events(p)).attrs for p in sorted(self.ledgers.glob("*.jsonl"))]


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: Provider) -> Iterator[Gw]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REAL")
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "ledgers: runs\n"
        "upstreams:\n  anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}\n"
        f"oidc: {{issuer: '{ISSUER}', audience: '{CLIENT_ID}', allow: [user-1, user-2]}}\n"
    )
    key = add_principal(path, "bob@corp")
    cfg = load_config(path)
    sessions = Sessions(cfg.ledgers, Signer.generate(), idle=900)
    reply: dict[str, Any] = {"id": "m1", "type": "message", "role": "assistant", "content": []}
    app = create_app(
        cfg, sessions, httpx2.MockTransport(lambda _: httpx2.Response(200, json=reply))
    )
    live: Live = app.state.live
    assert live.oidc is not None
    app.state.live = dataclasses.replace(live, oidc=OidcVerifier(live.oidc.cfg, idp.fetch))
    with TestClient(app) as client:
        yield Gw(client, key, cfg.ledgers, idp)
    assert sessions.close_all(timeout=1) == 0


def test_a_signed_in_user_is_recorded_by_subject_across_token_refreshes(gw: Gw) -> None:
    assert gw.ask(gw.idp.token()).status_code == 200
    assert gw.ask(gw.idp.token(iat=int(time.time()) + 5)).status_code == 200  # refreshed
    (start,) = gw.starts()  # one session: the subject, not the token, keys it
    assert start["principal.id"] == "user-1" and start["principal.auth"] == "oidc"
    assert start["principal.issuer"] == ISSUER and start["principal.name"] == "alice@corp.example"
    assert "principal.key_id" not in start


def test_the_gateway_refuses_bad_or_unlisted_tokens_and_keeps_issued_keys(gw: Gw) -> None:
    refused = gw.ask(gw.idp.token(aud="another-app"))
    assert refused.status_code == 401 and "audience" in refused.json()["error"]["message"].lower()
    unlisted = gw.ask(gw.idp.token(sub="mallory"))
    assert unlisted.status_code == 403 and "mallory" in unlisted.json()["error"]["message"]
    assert gw.ask("not-a-jwt").json()["error"]["message"] == "unknown seatbelt key"
    assert gw.ask(gw.key).status_code == 200  # an issued key still works beside OIDC
    models = gw.client.get(
        "/v1/models",
        headers={"authorization": f"Bearer {gw.idp.token()}", "anthropic-version": "1"},
    )
    assert models.status_code == 200
    ids = [s["principal.id"] for s in gw.starts()]
    assert ids == ["bob@corp"]  # refused requests opened no session


def test_a_reload_keeps_the_fetched_keys_when_oidc_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_text(
        f"ledgers: runs\nupstreams: {{}}\noidc: {{issuer: '{ISSUER}', audience: '{CLIENT_ID}'}}\n"
    )
    cfg = load_config(path)
    first = Live.of(cfg)
    assert Live.of(cfg, previous=first).oidc is first.oidc
    changed = cfg.model_copy(update={"oidc": _cfg(allow=["user-1"])})
    assert Live.of(changed, previous=first).oidc is not first.oidc
    assert Live.of(cfg.model_copy(update={"oidc": None}), previous=first).oidc is None
