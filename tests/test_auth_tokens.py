from __future__ import annotations

import base64

from app.auth.tokens import (
    issue_opaque_token,
    issue_verification_code,
    token_digest,
    verification_code_digest,
)


def test_opaque_tokens_have_at_least_256_bits_of_entropy() -> None:
    issued = issue_opaque_token()
    padded = issued.value + "=" * (-len(issued.value) % 4)
    decoded = base64.urlsafe_b64decode(padded)

    assert len(decoded) >= 32
    assert issued.digest == token_digest(issued.value)
    assert len(issued.digest) == 64
    assert issued.value not in issued.digest


def test_each_opaque_token_and_digest_is_unique() -> None:
    issued = [issue_opaque_token() for _ in range(100)]

    assert len({item.value for item in issued}) == 100
    assert len({item.digest for item in issued}) == 100


def test_token_digest_is_stable_without_retaining_the_secret() -> None:
    issued = issue_opaque_token()

    assert token_digest(issued.value) == issued.digest
    assert token_digest(issued.value + "x") != issued.digest


def test_verification_code_keeps_leading_zero_and_only_stores_hmac(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.auth.tokens.secrets.randbelow", lambda _: 7)
    issued = issue_verification_code(
        email="user@example.com",
        secret=b"v" * 32,
    )

    assert issued.value == "000007"
    assert issued.value not in issued.digest
    assert issued.digest == verification_code_digest(
        email="user@example.com",
        code="000007",
        secret=b"v" * 32,
    )


def test_verification_code_digest_is_bound_to_email_code_and_secret() -> None:
    digest = verification_code_digest(
        email="user@example.com",
        code="123456",
        secret=b"a" * 32,
    )

    assert digest != verification_code_digest(
        email="other@example.com",
        code="123456",
        secret=b"a" * 32,
    )
    assert digest != verification_code_digest(
        email="user@example.com",
        code="654321",
        secret=b"a" * 32,
    )
    assert digest != verification_code_digest(
        email="user@example.com",
        code="123456",
        secret=b"b" * 32,
    )
