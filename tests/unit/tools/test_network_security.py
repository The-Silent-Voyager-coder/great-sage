"""Outbound network target validation (SSRF guard) tests."""

from __future__ import annotations

import pytest

from greatsage.tools import network_security
from greatsage.tools.network_security import (
    NetworkTargetError,
    _ValidatingRedirectHandler,
    ensure_public_url,
)


def _resolver(*addresses: str):
    def resolve(host: str, port: int | None = None) -> tuple[str, ...]:
        return addresses

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",
        "http://192.0.2.1/",
    ],
)
def test_private_or_reserved_literals_are_rejected(url: str) -> None:
    with pytest.raises(NetworkTargetError, match="non-public|only absolute"):
        ensure_public_url(url)


def test_private_dns_answer_is_rejected() -> None:
    with pytest.raises(NetworkTargetError, match="non-public"):
        ensure_public_url(
            "https://internal.example/",
            resolver=_resolver("10.0.0.5"),
        )


def test_mixed_dns_answer_is_rejected() -> None:
    with pytest.raises(NetworkTargetError, match="non-public"):
        ensure_public_url(
            "https://mixed.example/",
            resolver=_resolver("93.184.216.34", "127.0.0.1"),
        )


def test_public_target_is_allowed() -> None:
    ensure_public_url(
        "https://example.com/docs",
        resolver=_resolver("93.184.216.34"),
    )


def test_credentials_and_bad_ports_are_rejected() -> None:
    with pytest.raises(NetworkTargetError, match="credentials"):
        ensure_public_url("https://user:pass@example.com/")
    with pytest.raises(NetworkTargetError, match="malformed"):
        ensure_public_url("https://example.com:not-a-port/")


def test_redirect_handler_validates_before_following() -> None:
    handler = _ValidatingRedirectHandler(network_security.ensure_public_url)
    with pytest.raises(NetworkTargetError, match="non-public"):
        handler.redirect_request(
            None,  # type: ignore[arg-type]
            None,
            302,
            "Found",
            {},
            "http://127.0.0.1/",
        )
