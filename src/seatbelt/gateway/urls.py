"""Which URLs seatbelt sends keys and prompts to: https://, or http:// only where nothing
crosses a network. Stdlib only: `seatbelt run` checks its config before it imports any
server code."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

# what an error says a URL must be
HTTPS = "https://, or http:// to this machine (127.0.0.1, ::1 or localhost)"


def _loopback(host: str) -> bool:
    """`host` is this machine: `localhost`, or an address in 127.0.0.0/8 or ::1."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _scheme_host(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. an unclosed [ in the host
        return "", ""
    return parts.scheme, parts.hostname or ""


def plain_http(url: str) -> bool:
    """`url` is http:// to another machine: what is sent to it crosses the network in the
    clear."""
    scheme, host = _scheme_host(url)
    return scheme == "http" and bool(host) and not _loopback(host)


def allowed(url: str, allow_http: bool = False) -> bool:
    """`url` is https://, or http:// to this machine, which never leaves it. With
    `allow_http`, http:// to any host: for a hop its operator knows is private."""
    scheme, host = _scheme_host(url)
    if not host:
        return False
    return scheme == "https" or (scheme == "http" and (allow_http or _loopback(host)))
