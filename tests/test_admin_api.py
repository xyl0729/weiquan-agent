from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.low_level import Type
from fastapi.testclient import TestClient

from app.admin.service import AdminService, InMemoryAdminAuditStore
from app.auth.passwords import PasswordManager
from app.auth.service import AuthService
from app.auth.store import InMemoryAuthStore
from app.config import Settings
from app.health.service import ProviderHealthService
from app.integrations.captcha import DevelopmentCaptchaVerifier
from app.integrations.directmail import InMemoryMailSender
from app.limits.reservations import InMemoryQuotaStore, QuotaService
from app.main import create_app
from app.privacy.policy import PrivacyPolicy
from app.providers.health import InMemoryProviderHealthStore


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
ORIGIN = "http://testserver"
PASSWORD = "password-12345"


def _application() -> tuple[
    object,
    AuthService,
    InMemoryAuthStore,
    InMemoryAdminAuditStore,
]:
    settings = Settings(
        _env_file=None,
        deployment_mode="test",
        public_base_url=ORIGIN,
        cors_origins=ORIGIN,
        cookie_secure=False,
    )
    auth_store = InMemoryAuthStore()
    passwords = PasswordManager(
        PasswordHasher(
            time_cost=1,
            memory_cost=8192,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )
    auth = AuthService(
        store=auth_store,
        passwords=passwords,
        mailer=InMemoryMailSender(),
        captcha=DevelopmentCaptchaVerifier(),
        policy=PrivacyPolicy(
            version=settings.privacy_policy_version,
            text="测试隐私政策",
        ),
        public_base_url=ORIGIN,
        rate_limit_secret=b"a" * 32,
        now=lambda: NOW,
    )
    quota = QuotaService(InMemoryQuotaStore(), now=lambda: NOW)
    health = ProviderHealthService(
        InMemoryProviderHealthStore(),
        now=lambda: NOW,
    )
    audit = InMemoryAdminAuditStore()
    admin = AdminService(
        auth_store=auth_store,
        quota_service=quota,
        provider_health=health,
        audit_store=audit,
        now=lambda: NOW,
    )
    application = create_app(settings)
    application.state.auth_service = auth
    application.state.quota_service = quota
    application.state.provider_health_service = health
    application.state.admin_service = admin
    return application, auth, auth_store, audit


def _add_user(
    auth: AuthService,
    store: InMemoryAuthStore,
    *,
    email: str,
    role: str,
) -> str:
    decision = store.create_verified_user(
        email=email,
        password_hash=auth.passwords.hash(PASSWORD),
        role=role,
        policy_version=auth.policy.version,
        now=NOW,
        pending_ttl=timedelta(hours=24),
    )
    assert decision.user is not None
    return decision.user.id


def _login(client: TestClient, *, email: str) -> str:
    logged_in = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={"email": email, "password": PASSWORD},
    )
    assert logged_in.status_code == 200
    csrf = client.get("/api/auth/csrf")
    assert csrf.status_code == 200
    return csrf.json()["csrf_token"]


def test_admin_api_rejects_anonymous_and_ordinary_users() -> None:
    application, auth, store, _ = _application()
    _add_user(
        auth,
        store,
        email="user@example.com",
        role="user",
    )
    anonymous = TestClient(application)
    ordinary = TestClient(application)
    _login(ordinary, email="user@example.com")

    anonymous_response = anonymous.get("/api/admin/diagnostics")
    ordinary_response = ordinary.get("/api/admin/diagnostics")

    assert anonymous_response.status_code == 401
    assert ordinary_response.status_code == 403
    assert ordinary_response.json()["detail"]["code"] == "admin_required"


def test_admin_api_returns_no_content_and_audits_state_changes() -> None:
    application, auth, store, audit = _application()
    _add_user(
        auth,
        store,
        email="admin@example.com",
        role="admin",
    )
    user_id = _add_user(
        auth,
        store,
        email="user@example.com",
        role="user",
    )
    client = TestClient(application)
    csrf = _login(client, email="admin@example.com")

    diagnostics = client.get("/api/admin/diagnostics")
    no_csrf = client.post(
        f"/api/admin/users/{user_id}/revoke-sessions",
    )
    revoked = client.post(
        f"/api/admin/users/{user_id}/revoke-sessions",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    disabled = client.post(
        f"/api/admin/users/{user_id}/disable",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )

    assert diagnostics.status_code == 200
    assert no_csrf.status_code == 403
    assert revoked.status_code == 200
    assert disabled.status_code == 200
    assert [event.action for event in audit.events] == [
        "revoke_sessions",
        "disable_user",
    ]
    serialized = diagnostics.text.casefold()
    for forbidden in (
        "admin@example.com",
        "user@example.com",
        "user_message",
        "response",
        "confirmed_text",
        "prompt",
        "attachment",
    ):
        assert forbidden not in serialized

