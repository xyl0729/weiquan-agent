from __future__ import annotations

import hashlib
import hmac

import pytest
from starlette.requests import Request

from app.security.network import (
    client_ip_from_request,
    hmac_ip_digest,
    normalize_ip_address,
)


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw_headers,
            "client": (peer, 12345),
            "scheme": "https",
            "server": ("app", 8001),
        }
    )


def test_ip_normalization_collapses_equivalent_addresses() -> None:
    assert normalize_ip_address(" 203.0.113.9 ") == "203.0.113.9"
    assert normalize_ip_address("::ffff:203.0.113.9") == "203.0.113.9"
    assert normalize_ip_address("2001:0db8:0:0::1") == "2001:db8::1"

    with pytest.raises(ValueError):
        normalize_ip_address("not-an-ip")


def test_proxy_headers_are_only_trusted_from_loopback() -> None:
    forged = _request(
        peer="198.51.100.7",
        headers={
            "X-Real-IP": "203.0.113.8",
            "X-Forwarded-For": "203.0.113.9",
        },
    )
    proxied = _request(
        peer="127.0.0.1",
        headers={
            "X-Real-IP": "203.0.113.8",
            "X-Forwarded-For": "198.51.100.2, 203.0.113.9",
        },
    )
    forwarded_only = _request(
        peer="::1",
        headers={
            "X-Forwarded-For": "198.51.100.2, 203.0.113.9",
        },
    )

    assert client_ip_from_request(forged) == "198.51.100.7"
    assert client_ip_from_request(proxied) == "203.0.113.8"
    assert client_ip_from_request(forwarded_only) == "203.0.113.9"


def test_invalid_loopback_proxy_header_falls_back_to_peer() -> None:
    request = _request(
        peer="127.0.0.1",
        headers={"X-Real-IP": "invalid"},
    )

    assert client_ip_from_request(request) == "127.0.0.1"


def test_ip_digest_uses_domain_separated_hmac() -> None:
    secret = b"i" * 32
    digest = hmac_ip_digest("::ffff:203.0.113.9", secret)
    expected = hmac.new(
        secret,
        b"weiquan-trial-ip-v1:203.0.113.9",
        hashlib.sha256,
    ).hexdigest()

    assert digest == expected
    assert "203.0.113.9" not in digest
    assert len(digest) == 64

