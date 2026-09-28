"""Sign-in through an OpenID Connect provider, as Claude Desktop does with
`inferenceGatewayOidc`: the app signs the user in with the provider and sends the provider's
token as `Authorization: Bearer` on every request (claude.com/docs/third-party/claude-desktop/
gateway). The gateway checks it offline against the provider's published keys: signature,
issuer, audience and expiry. Checking the audience is what stops any other token from the same
tenant being accepted."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, cast

import httpx2
import jwt

from seatbelt.gateway.config import OidcConfig

JWKS_LIFESPAN = 300.0  # seconds a fetched key set is trusted before it is fetched again
REFETCH_COOLDOWN = 30.0  # an unknown key id refetches at most this often


class OidcError(Exception):
    pass


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("eyJ")  # base64url of '{"'


type Fetch = Callable[[str], dict[str, Any]]


def _fetch_json(url: str) -> dict[str, Any]:
    try:
        resp = httpx2.get(url, timeout=10, follow_redirects=False)
        resp.raise_for_status()
        data: Any = resp.json()  # provider JSON
    except (httpx2.HTTPError, ValueError) as exc:
        raise OidcError(f"{url}: {exc}") from exc
    if not isinstance(data, dict):
        raise OidcError(f"{url}: not a JSON object")
    return cast(dict[str, Any], data)


class OidcVerifier:
    """Thread-safe. The key set is fetched on first use (from `jwks_url`, or the issuer's
    discovery document), kept for `JWKS_LIFESPAN`, and fetched again early when a token names a
    key it does not hold: the provider has rotated its keys."""

    def __init__(
        self, cfg: OidcConfig, fetch: Fetch = _fetch_json, clock: Callable[[], float] = time.time
    ) -> None:
        self.cfg = cfg
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._jwks_url = cfg.jwks_url
        self._keys: jwt.PyJWKSet | None = None
        self._fetched = 0.0

    def _jwks_uri(self) -> str:
        if self._jwks_url is None:
            doc = self._fetch(self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration")
            uri = doc.get("jwks_uri")
            if not isinstance(uri, str):
                raise OidcError("the provider's discovery document names no jwks_uri")
            self._jwks_url = uri
        return self._jwks_url

    def _key(self, kid: str | None) -> jwt.PyJWK:
        with self._lock:
            now = self._clock()
            for attempt in (0, 1):
                stale = self._keys is None or now - self._fetched > JWKS_LIFESPAN
                if stale or (attempt and now - self._fetched > REFETCH_COOLDOWN):
                    try:
                        self._keys = jwt.PyJWKSet.from_dict(self._fetch(self._jwks_uri()))
                    except jwt.PyJWKSetError as exc:
                        raise OidcError(f"the provider's key set: {exc}") from exc
                    self._fetched = now
                assert self._keys is not None  # noqa: S101 - set just above when missing
                for key in self._keys.keys:
                    if kid is None or key.key_id == kid:
                        return key
            raise OidcError(f"no key {kid!r} in the provider's key set")

    def verify(self, token: str) -> dict[str, Any]:
        """The token's claims, once its signature, issuer, audience and expiry check out.
        Raises OidcError saying why not."""
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            key = self._key(kid if isinstance(kid, str) else None)
            return jwt.decode(
                token,
                key,
                algorithms=self.cfg.algorithms,
                audience=self.cfg.audience,
                issuer=self.cfg.issuer,
                leeway=self.cfg.leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(str(exc)) from exc
