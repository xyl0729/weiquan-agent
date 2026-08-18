from __future__ import annotations

import pytest

from app.auth.errors import (
    AuthenticationFailedError,
    AuthRateLimitError,
    CsrfInvalidError,
)
from app.security.network import client_ip_from_request
from tests.test_auth_api import (
    ORIGIN,
    _client,
    _register_and_verify,
)
from tests.test_auth_service import (
    _activate,
    _service,
)
from tests.test_network_identity import _request


def test_passwords_use_argon2id_and_never_store_plaintext() -> None:
    service, store, mailer, _, _ = _service()
    _activate(service, mailer)

    user = store.get_user_by_email("user@example.com")

    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert "password-12345" not in user.password_hash


def test_sessions_are_independent_and_csrf_tokens_rotate() -> None:
    service, _, mailer, _, _ = _service()
    user_id = _activate(service, mailer)
    first_session = service.login(
        email="user@example.com",
        password="password-12345",
        client_key="198.51.100.10",
    )
    second_session = service.login(
        email="user@example.com",
        password="password-12345",
        client_key="198.51.100.11",
    )

    first_csrf = service.issue_csrf(first_session.session_token)
    rotated_csrf = service.issue_csrf(first_session.session_token)

    assert first_session.session_token != second_session.session_token
    assert service.authenticate(first_session.session_token).user.id == user_id
    assert service.authenticate(second_session.session_token).user.id == user_id
    assert first_csrf != rotated_csrf
    assert rotated_csrf not in {
        first_session.session_token,
        second_session.session_token,
    }
    with pytest.raises(CsrfInvalidError):
        service.validate_csrf(first_session.session_token, first_csrf)
    assert (
        service.validate_csrf(
            first_session.session_token,
            rotated_csrf,
        ).user.id
        == user_id
    )


def test_production_cookie_and_same_origin_controls_fail_closed() -> None:
    client, mailer = _client(secure_cookie=True)
    _register_and_verify(client, mailer)

    login = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "email": "user@example.com",
            "password": "password-12345",
        },
    )
    cookie = login.headers["set-cookie"]
    no_origin = client.post("/api/auth/logout")
    foreign_origin = client.post(
        "/api/auth/logout",
        headers={
            "Origin": "https://attacker.example",
            "X-CSRF-Token": "forged",
        },
    )

    assert login.status_code == 200
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert no_origin.status_code == foreign_origin.status_code == 403
    assert no_origin.json() == foreign_origin.json()
    assert no_origin.json()["detail"]["code"] == "same_origin_required"


def test_login_and_password_reset_do_not_enumerate_accounts() -> None:
    client, mailer = _client()
    _register_and_verify(client, mailer)

    missing_login = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "email": "missing@example.com",
            "password": "password-12345",
        },
    )
    wrong_password = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "email": "user@example.com",
            "password": "wrong-password",
        },
    )
    reset_responses = [
        client.post(
            "/api/auth/forgot-password",
            headers={"Origin": ORIGIN},
            json={"email": email},
        )
        for email in (
            "user@example.com",
            "missing@example.com",
            "not-an-email",
        )
    ]

    assert missing_login.status_code == wrong_password.status_code == 401
    assert missing_login.json() == wrong_password.json()
    assert all(response.status_code == 202 for response in reset_responses)
    assert len({response.text for response in reset_responses}) == 1
    assert len(mailer.reset_url) > 0


def test_untrusted_proxy_headers_cannot_bypass_auth_rate_limit() -> None:
    service, _, _, _, _ = _service()
    first = _request(
        peer="198.51.100.7",
        headers={"X-Forwarded-For": "203.0.113.1"},
    )
    second = _request(
        peer="198.51.100.7",
        headers={"X-Forwarded-For": "203.0.113.2"},
    )
    first_key = client_ip_from_request(first)
    second_key = client_ip_from_request(second)

    assert first_key == second_key == "198.51.100.7"
    for _ in range(20):
        with pytest.raises(AuthenticationFailedError):
            service.login(
                email="missing@example.com",
                password="password-12345",
                client_key=first_key,
            )
    with pytest.raises(AuthRateLimitError):
        service.login(
            email="missing@example.com",
            password="password-12345",
            client_key=second_key,
        )
