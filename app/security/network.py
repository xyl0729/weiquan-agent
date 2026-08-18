from __future__ import annotations

import hashlib
import hmac
import ipaddress

from starlette.requests import Request


def normalize_ip_address(value: str) -> str:
    address = ipaddress.ip_address(value.strip())
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return str(mapped)
    return address.compressed


def client_ip_from_request(request: Request) -> str:
    peer_value = request.client.host if request.client else "0.0.0.0"
    try:
        peer = normalize_ip_address(peer_value)
    except ValueError:
        return "0.0.0.0"

    if not ipaddress.ip_address(peer).is_loopback:
        return peer

    real_ip = request.headers.get("x-real-ip")
    if real_ip is not None:
        try:
            return normalize_ip_address(real_ip)
        except ValueError:
            return peer

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        candidate = forwarded.rsplit(",", 1)[-1].strip()
        try:
            return normalize_ip_address(candidate)
        except ValueError:
            return peer
    return peer


def hmac_ip_digest(ip_address: str, secret: bytes) -> str:
    if len(secret) < 32:
        raise ValueError("IP HMAC 密钥至少需要 32 字节")
    normalized = normalize_ip_address(ip_address)
    payload = f"weiquan-trial-ip-v1:{normalized}".encode("ascii")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()

