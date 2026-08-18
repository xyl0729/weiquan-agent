from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IssuedToken:
    value: str
    digest: str


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_opaque_token(*, byte_length: int = 32) -> IssuedToken:
    if byte_length < 32:
        raise ValueError("令牌随机强度不能低于 256 位")
    value = secrets.token_urlsafe(byte_length)
    return IssuedToken(value=value, digest=token_digest(value))


def verification_code_digest(
    *,
    email: str,
    code: str,
    secret: bytes,
) -> str:
    if len(secret) < 32:
        raise ValueError("邮箱验证码密钥不能短于 32 字节")
    return hmac.new(
        secret,
        f"{email}\0{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_verification_code(
    *,
    email: str,
    secret: bytes,
) -> IssuedToken:
    value = f"{secrets.randbelow(1_000_000):06d}"
    return IssuedToken(
        value=value,
        digest=verification_code_digest(
            email=email,
            code=value,
            secret=secret,
        ),
    )
