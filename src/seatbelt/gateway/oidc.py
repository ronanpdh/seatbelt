"""Sign-in through an OpenID Connect provider, as Claude Desktop does with
`inferenceGatewayOidc`: the app signs the user in with the provider and sends the provider's
token as `Authorization: Bearer` on every request (claude.com/docs/third-party/claude-desktop/
gateway). The gateway checks it offline against the provider's published keys: signature,
issuer, audience and expiry. Checking the audience is what stops any other token from the same
tenant being accepted."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any, cast

import httpx2
import jwt

from seatbelt.gateway.config import OidcConfig

JWKS_LIFESPAN = 300.0  # seconds a fetched key set is trusted before it is fetched again
# seconds past its lifespan a key set is still used while fetching a new one fails: an outage
# at the provider does not sign everyone out at once
JWKS_GRACE = 3600.0
REFETCH_COOLDOWN = 30.0  # a fetch, failed or for an unknown key id, is tried at most this often
FETCH_TIMEOUT = 5.0  # seconds

_log = logging.getLogger(__name__)


class OidcError(Exception):
    pass


class OidcUnavailable(OidcError):
    """The provider's keys could not be fetched. The message is for the log: it names the
    provider's URLs and the network error, which a client has no need to see."""


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("eyJ")  # base64url of '{"'


type Fetch = Callable[[str], dict[str, Any]]


def _fetch_json(url: str) -> dict[str, Any]:
    try:
        resp = httpx2.get(url, timeout=FETCH_TIMEOUT, follow_redirects=False)
        resp.raise_for_status()
        data: Any = resp.json()  # provider JSON
    except (httpx2.HTTPError, ValueError) as exc:
        raise OidcUnavailable(f"{url}: {exc}") from exc
    if not isinstance(data, dict):
        raise OidcUnavailable(f"{url}: not a JSON object")
    return cast(dict[str, Any], data)


class OidcVerifier:
    """Thread-safe. The key set is fetched on first use (from `jwks_url`, or the issuer's
    discovery document), kept for `JWKS_LIFESPAN`, and fetched again early when a token names a
    key it does not hold: the provider has rotated its keys. One thread fetches at a time,
    while the others go on with the keys held; when a fetch fails, those keys are used for up
    to `JWKS_GRACE` more, and the fetch is not tried again for `REFETCH_COOLDOWN`."""

    def __init__(
        self, cfg: OidcConfig, fetch: Fetch = _fetch_json, clock: Callable[[], float] = time.time
    ) -> None:
        self.cfg = cfg
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._jwks_url = cfg.jwks_url
        self._fetch_done = threading.Condition(self._lock)
        self._keys: jwt.PyJWKSet | None = None
        self._fetched = 0.0  # when the keys held were fetched
        self._tried = -math.inf  # when a fetch last started, whether it worked or not
        self._fetching = False

    def _jwks_uri(self) -> str:
        if self._jwks_url is None:
            doc = self._fetch(self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration")
            uri = doc.get("jwks_uri")
            if not isinstance(uri, str):
                raise OidcUnavailable("the provider's discovery document names no jwks_uri")
            self._jwks_url = uri
        return self._jwks_url

    def _key_set(self, refresh: bool) -> jwt.PyJWKSet:
        """The keys to check a token with, fetched first when none are held, when they are
        past their lifespan, or on `refresh` (a token names a key they lack)."""
        with self._lock:
            while True:
                now = self._clock()
                held = self._keys
                age = now - self._fetched
                usable = held is not None and age <= JWKS_LIFESPAN + JWKS_GRACE
                due = held is None or age > JWKS_LIFESPAN or refresh
                if due and self._fetching and not usable:
                    self._fetch_done.wait()  # nothing to go on with meanwhile
                    continue
                if due and not self._fetching and now - self._tried > REFETCH_COOLDOWN:
                    self._fetching, self._tried = True, now
                    break
                if held is not None and usable:
                    return held
                raise OidcUnavailable("the provider's key set could not be fetched lately")
        try:  # outside the lock: no request waits on the network while keys are held
            try:
                keys = jwt.PyJWKSet.from_dict(self._fetch(self._jwks_uri()))
            except jwt.PyJWKSetError as exc:
                raise OidcUnavailable(f"the provider's key set: {exc}") from exc
        except OidcUnavailable as exc:
            with self._lock:
                self._fetching = False
                self._fetch_done.notify_all()
                held, age = self._keys, now - self._fetched
            if held is not None and age <= JWKS_LIFESPAN + JWKS_GRACE:
                _log.warning(
                    "fetching the sign-in provider's keys failed; using those fetched %.0f s "
                    "ago: %s",
                    age,
                    exc,
                )
                return held
            _log.warning("fetching the sign-in provider's keys failed: %s", exc)
            raise
        except BaseException:
            with self._lock:
                self._fetching = False
                self._fetch_done.notify_all()
            raise
        with self._lock:
            self._keys, self._fetched, self._fetching = keys, now, False
            self._fetch_done.notify_all()
        return keys

    def _key(self, kid: str | None) -> jwt.PyJWK:
        for refresh in (False, True):
            for key in self._key_set(refresh).keys:
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
