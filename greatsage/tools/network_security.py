"""Network target validation for outbound HTTP tools.

The assistant can read untrusted web content, so ``network.fetch`` and
``network.webfetch`` must not become a bridge to loopback, private, link-local,
or cloud-metadata services. This module keeps that policy in one place:
resolve the host, reject every non-global address, and re-check each redirect
before urllib follows it.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

Resolver = Callable[[str, int | None], Iterable[str]]


class NetworkTargetError(ValueError):
    """The requested URL resolves to a destination the network tools refuse."""


def resolve_host(host: str, port: int | None = None) -> tuple[str, ...]:
    """Resolve a hostname to deduplicated address strings (stdlib only)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NetworkTargetError(f"could not resolve host {host!r}") from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise NetworkTargetError(f"host {host!r} resolved to no addresses")
    return tuple(addresses)


def _addresses_for_host(
    host: str, port: int | None, resolver: Resolver
) -> tuple[str, ...]:
    literal = host.split("%", 1)[0]
    try:
        return (str(ipaddress.ip_address(literal)),)
    except ValueError:
        return tuple(resolver(host, port))


def _is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_global


def ensure_public_url(
    url: str, *, resolver: Resolver | None = None
) -> None:
    """Raise NetworkTargetError unless every resolved address is public.

    ``resolver`` is injectable so tests never perform DNS or network I/O.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise NetworkTargetError(f"malformed url: {exc}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise NetworkTargetError("only absolute http(s) urls are allowed")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise NetworkTargetError("urls with credentials are not allowed")
    target_port = port or (443 if parsed.scheme == "https" else 80)
    addresses = _addresses_for_host(
        parsed.hostname, target_port, resolver or resolve_host
    )
    if not addresses:
        raise NetworkTargetError(f"host {parsed.hostname!r} resolved to no addresses")
    for address in addresses:
        if not _is_public(address):
            raise NetworkTargetError(
                f"host {parsed.hostname!r} resolves to non-public address {address}"
            )


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect target before urllib follows it."""

    def __init__(self, validator: Callable[[str], None]) -> None:
        self._validator = validator

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public_url(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> Any:
    """Open an already-validated request, re-validating every redirect."""
    opener = urllib.request.build_opener(
        _ValidatingRedirectHandler(ensure_public_url)
    )
    response = opener.open(request, timeout=timeout)
    # Redirects are validated before they are followed; this final check is
    # defense in depth for a handler that returns an unexpected URL.
    try:
        final_url = response.geturl()
    except Exception:  # pragma: no cover - minimal response objects
        return response
    try:
        ensure_public_url(str(final_url))
    except NetworkTargetError:
        response.close()
        raise
    return response
