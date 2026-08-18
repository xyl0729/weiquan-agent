from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import initialize_auth_dependencies
from app.auth.passwords import PasswordManager
from app.auth.service import AuthService
from app.auth.store import InMemoryAuthStore
from app.config import Settings
from app.integrations.captcha import (
    AliyunCaptchaVerifier,
    DisabledCaptchaVerifier,
)
from app.integrations.directmail import (
    AliyunDirectMailSender,
    InMemoryMailSender,
)
from app.main import create_app
from app.privacy.policy import PrivacyPolicy


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
ORIGIN = "http://testserver"


class ApiMailer:
    def __init__(self) -> None:
        self.verification_code = ""
        self.reset_url = ""

    def send_verification(
        self,
        *,
        to_email: str,
        verification_code: str,
    ) -> None:
        del to_email
        self.verification_code = verification_code

    def send_password_reset(
        self,
        *,
        to_email: str,
        reset_url: str,
    ) -> None:
        del to_email
        self.reset_url = reset_url


class ApiCaptcha:
    def verify(self, *, token: str, remote_ip: str) -> bool:
        del remote_ip
        return token == "captcha-ok"


def _client(
    *,
    secure_cookie: bool = False,
    captcha: object | None = None,
) -> tuple[TestClient, ApiMailer]:
    settings = Settings(
        _env_file=None,
        deployment_mode="test",
        public_base_url=ORIGIN,
        cors_origins=ORIGIN,
        cookie_secure=secure_cookie,
        privacy_policy_version="2026-08-10",
    )
    mailer = ApiMailer()
    service = AuthService(
        store=InMemoryAuthStore(),
        passwords=PasswordManager(
            PasswordHasher(
                time_cost=1,
                memory_cost=8192,
                parallelism=1,
                hash_len=16,
                salt_len=16,
                type=Type.ID,
            )
        ),
        mailer=mailer,
        captcha=captcha or ApiCaptcha(),  # type: ignore[arg-type]
        policy=PrivacyPolicy(
            version="2026-08-10",
            text="测试隐私政策正文",
        ),
        public_base_url=ORIGIN,
        rate_limit_secret=b"a" * 32,
        now=lambda: NOW,
    )
    application = create_app(settings)
    application.state.auth_service = service
    return TestClient(application), mailer


def _register_and_verify(
    client: TestClient,
    mailer: ApiMailer,
    *,
    email: str = "user@example.com",
) -> None:
    registered = client.post(
        "/api/auth/register",
        headers={"Origin": ORIGIN},
        json={
            "email": email,
            "password": "password-12345",
            "captcha_token": "captcha-ok",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )
    assert registered.status_code == 202
    verified = client.post(
        "/api/auth/verify",
        headers={"Origin": ORIGIN},
        json={
            "email": email,
            "code": mailer.verification_code,
        },
    )
    assert verified.status_code == 200


def test_register_verify_login_me_csrf_and_logout_flow() -> None:
    client, mailer = _client()
    _register_and_verify(client, mailer)

    logged_in = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "email": "user@example.com",
            "password": "password-12345",
        },
    )

    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["email"] == "user@example.com"
    cookie = logged_in.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=604800" in cookie
    assert client.get("/api/auth/me").status_code == 200

    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    missing = client.post("/api/auth/logout")
    cross_origin = client.post(
        "/api/auth/logout",
        headers={
            "Origin": "https://attacker.example",
            "X-CSRF-Token": csrf,
        },
    )
    logged_out = client.post(
        "/api/auth/logout",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
        },
    )

    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "same_origin_required"
    assert cross_origin.status_code == 403
    assert cross_origin.json()["detail"]["code"] == (
        "same_origin_required"
    )
    assert logged_out.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_auth_user_projection_includes_quota_and_privacy_state() -> None:
    client, mailer = _client()
    _register_and_verify(client, mailer)

    logged_in = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "email": "user@example.com",
            "password": "password-12345",
        },
    )

    assert logged_in.status_code == 200
    body = logged_in.json()
    assert set(body) == {
        "user",
        "quota",
        "privacy_version",
        "privacy_acceptance_required",
    }
    assert body["quota"]["remaining_daily"] == 10
    assert body["quota"]["remaining_monthly"] == 50
    assert body["quota"]["day_resets_at"].endswith("Z")
    assert body["quota"]["month_resets_at"].endswith("Z")
    assert body["privacy_version"] == "2026-08-10"
    assert body["privacy_acceptance_required"] is True

    me_before = client.get("/api/auth/me")
    assert me_before.status_code == 200
    assert me_before.json() == body

    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    accepted = client.post(
        "/api/privacy/accept",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
        },
        json={
            "context": "consultation",
            "policy_version": "2026-08-10",
        },
    )
    me_after = client.get("/api/auth/me")

    assert accepted.status_code == 200
    assert me_after.status_code == 200
    assert me_after.json()["privacy_acceptance_required"] is False
    assert me_after.json()["quota"] == body["quota"]


def test_production_cookie_attributes_and_credentialed_cors() -> None:
    client, mailer = _client(secure_cookie=True)
    _register_and_verify(client, mailer)

    response = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "email": "user@example.com",
            "password": "password-12345",
        },
    )
    preflight = client.options(
        "/api/auth/logout",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "content-type,x-csrf-token"
            ),
        },
    )

    assert "Secure" in response.headers["set-cookie"]
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert "x-csrf-token" in (
        preflight.headers["access-control-allow-headers"].lower()
    )


def test_login_failures_do_not_reveal_email_existence() -> None:
    client, mailer = _client()
    _register_and_verify(client, mailer)

    unknown = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "email": "missing@example.com",
            "password": "password-12345",
        },
    )
    wrong = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "email": "user@example.com",
            "password": "wrong-password",
        },
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.json()["detail"]["code"] == "invalid_credentials"


@pytest.mark.parametrize(
    ("password_length", "expected_status"),
    (
        (7, 422),
        (8, 202),
        (128, 202),
        (129, 422),
    ),
)
def test_register_password_length_boundary(
    password_length: int,
    expected_status: int,
) -> None:
    client, mailer = _client()

    response = client.post(
        "/api/auth/register",
        headers={"Origin": ORIGIN},
        json={
            "email": f"boundary-{password_length}@example.com",
            "password": "p" * password_length,
            "captcha_token": "captcha-ok",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )

    assert response.status_code == expected_status
    if expected_status == 202:
        assert response.json() == {"status": "accepted"}
        assert len(mailer.verification_code) == 6
    else:
        assert response.json()["detail"]["code"] == "request_validation"
        assert mailer.verification_code == ""


@pytest.mark.parametrize(
    ("password_length", "expected_status"),
    (
        (7, 422),
        (8, 204),
        (128, 204),
        (129, 422),
    ),
)
def test_reset_password_length_boundary(
    password_length: int,
    expected_status: int,
) -> None:
    client, mailer = _client()
    _register_and_verify(client, mailer)

    forgot = client.post(
        "/api/auth/forgot-password",
        headers={"Origin": ORIGIN},
        json={"email": "user@example.com"},
    )
    assert forgot.status_code == 202
    reset_token = parse_qs(
        urlsplit(mailer.reset_url).fragment
    )["token"][0]

    response = client.post(
        "/api/auth/reset-password",
        headers={"Origin": ORIGIN},
        json={
            "token": reset_token,
            "new_password": "n" * password_length,
        },
    )

    assert response.status_code == expected_status
    if expected_status == 422:
        assert response.json()["detail"]["code"] == "request_validation"


def test_public_auth_writes_require_same_origin_and_captcha() -> None:
    client, _ = _client()
    payload = {
        "email": "user@example.com",
        "password": "password-12345",
        "captcha_token": "captcha-ok",
        "privacy_version": "2026-08-10",
        "privacy_accepted": True,
    }

    no_origin = client.post("/api/auth/register", json=payload)
    bad_captcha = client.post(
        "/api/auth/register",
        headers={"Origin": ORIGIN},
        json={**payload, "captcha_token": "captcha-bad"},
    )

    assert no_origin.status_code == 403
    assert no_origin.json()["detail"]["code"] == "same_origin_required"
    assert bad_captcha.status_code == 422
    assert bad_captcha.json()["detail"]["code"] == "captcha_failed"


def test_register_allows_omitted_captcha_when_disabled() -> None:
    client, mailer = _client(captcha=DisabledCaptchaVerifier())

    response = client.post(
        "/api/auth/register",
        headers={"Origin": ORIGIN},
        json={
            "email": "no-captcha@example.com",
            "password": "password-12345",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )

    assert response.status_code == 202
    assert mailer.verification_code


@pytest.mark.parametrize(
    "code",
    ("12345", "1234567", "12a456", "１２３４５６"),
)
def test_verify_rejects_non_six_ascii_digit_codes(code: str) -> None:
    client, _ = _client()

    response = client.post(
        "/api/auth/verify",
        headers={"Origin": ORIGIN},
        json={"email": "user@example.com", "code": code},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation"


def test_captcha_config_is_disabled_outside_production() -> None:
    client, _ = _client()

    response = client.get("/api/auth/captcha-config")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "scene_id": "",
        "prefix": "",
        "region": "cn",
    }
    assert "access_key" not in response.text.casefold()
    assert "secret" not in response.text.casefold()


def test_captcha_config_exposes_only_public_production_fields() -> None:
    settings = Settings(
        _env_file=None,
        deployment_mode="test",
        public_base_url=ORIGIN,
        cors_origins=ORIGIN,
        captcha_enabled=True,
        captcha_scene_id="scene-public",
        captcha_prefix="prefix-public",
    ).model_copy(update={"deployment_mode": "production"})
    client = TestClient(create_app(settings))

    response = client.get("/api/auth/captcha-config")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "scene_id": "scene-public",
        "prefix": "prefix-public",
        "region": "cn",
    }
    assert set(response.json()) == {
        "enabled",
        "scene_id",
        "prefix",
        "region",
    }


def test_production_captcha_config_stays_private_when_disabled() -> None:
    settings = Settings(
        _env_file=None,
        deployment_mode="test",
        public_base_url=ORIGIN,
        cors_origins=ORIGIN,
        captcha_scene_id="unused-scene",
        captcha_prefix="unused-prefix",
    ).model_copy(update={"deployment_mode": "production"})
    client = TestClient(create_app(settings))

    response = client.get("/api/auth/captcha-config")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "scene_id": "",
        "prefix": "",
        "region": "cn",
    }


def test_disabled_production_never_builds_aliyun_captcha(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        deployment_mode="test",
        public_base_url=ORIGIN,
        cors_origins=ORIGIN,
        aliyun_access_key_id="access-key-id",
        aliyun_access_key_secret="access-key-secret",
        ip_hmac_secret="ip-hmac-secret-value-with-at-least-32-bytes",
    ).model_copy(update={"deployment_mode": "production"})
    application = FastAPI()
    application.state.settings = settings

    monkeypatch.setattr(
        "app.auth.dependencies.create_database_engine",
        lambda active_settings: object(),
    )
    monkeypatch.setattr(
        "app.auth.dependencies.PostgresAuthStore",
        lambda engine: InMemoryAuthStore(),
    )
    monkeypatch.setattr(
        AliyunDirectMailSender,
        "from_credentials",
        classmethod(
            lambda cls, **kwargs: InMemoryMailSender()
        ),
    )

    def unexpected_captcha(
        cls: object,
        **kwargs: object,
    ) -> object:
        del cls, kwargs
        raise AssertionError("disabled CAPTCHA built an Aliyun client")

    monkeypatch.setattr(
        AliyunCaptchaVerifier,
        "from_credentials",
        classmethod(unexpected_captcha),
    )

    service = initialize_auth_dependencies(application)

    assert isinstance(service.captcha, DisabledCaptchaVerifier)
    assert isinstance(
        application.state.auth_captcha,
        DisabledCaptchaVerifier,
    )
