from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.admin.models import AdminDiagnostics
from app.admin.service import (
    AdminService,
    InMemoryAdminAuditStore,
)
from app.agent.models import UsageInfo
from app.auth.store import InMemoryAuthStore
from app.health.service import ProviderHealthService
from app.limits.reservations import InMemoryQuotaStore, QuotaService
from app.providers.health import InMemoryProviderHealthStore


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def _create_user(
    store: InMemoryAuthStore,
    *,
    email: str,
    role: str = "user",
) -> str:
    decision = store.create_verified_user(
        email=email,
        password_hash="argon2-test-hash",
        role=role,
        policy_version="2026-08-10",
        now=NOW,
        pending_ttl=timedelta(hours=24),
    )
    assert decision.user is not None
    return decision.user.id


def _service() -> tuple[
    AdminService,
    InMemoryAuthStore,
    InMemoryAdminAuditStore,
]:
    auth = InMemoryAuthStore()
    quota = QuotaService(
        InMemoryQuotaStore(),
        now=lambda: NOW,
    )
    health = ProviderHealthService(
        InMemoryProviderHealthStore(),
        now=lambda: NOW,
    )
    audit = InMemoryAdminAuditStore()
    return (
        AdminService(
            auth_store=auth,
            quota_service=quota,
            provider_health=health,
            audit_store=audit,
            now=lambda: NOW,
        ),
        auth,
        audit,
    )


def test_admin_diagnostics_use_a_content_free_projection() -> None:
    service, auth, _ = _service()
    user_id = _create_user(auth, email="user@example.com")
    service.provider_health.record(
        provider="deepseek",
        model="deepseek-chat",
        outcome="timeout",
        duration_ms=1200,
        usage=UsageInfo(),
        occurred_at=NOW,
    )

    diagnostics = service.diagnostics()
    payload = diagnostics.model_dump(mode="json")

    assert isinstance(diagnostics, AdminDiagnostics)
    assert payload["accounts"] == [
        {
            "user_id": user_id,
            "status": "active",
            "email_verified": True,
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
            "quota": {
                "remaining_daily": 10,
                "remaining_monthly": 50,
                "day_resets_at": (
                    "2026-08-10T16:00:00Z"
                ),
                "month_resets_at": (
                    "2026-08-31T16:00:00Z"
                ),
            },
        }
    ]
    assert payload["provider"] == {
        "provider": "deepseek",
        "status": "healthy",
        "sample_count": 1,
        "success_count": 0,
        "error_categories": ["timeout"],
        "last_result_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    serialized = diagnostics.model_dump_json().casefold()
    for forbidden in (
        "user@example.com",
        "user_message",
        "consultation",
        "model_output",
        "confirmed_text",
        "prompt",
        "original_name",
    ):
        assert forbidden not in serialized


def test_admin_actions_revoke_sessions_and_persist_content_free_audit() -> None:
    service, auth, audit = _service()
    admin_id = _create_user(
        auth,
        email="admin@example.com",
        role="admin",
    )
    user_id = _create_user(auth, email="user@example.com")
    auth.create_session(
        user_id=user_id,
        token_digest="a" * 64,
        now=NOW,
        expires_at=NOW + timedelta(days=7),
    )

    revoked = service.revoke_user_sessions(
        admin_id=admin_id,
        target_user_id=user_id,
    )
    disabled = service.disable_user(
        admin_id=admin_id,
        target_user_id=user_id,
    )

    assert revoked.result == "succeeded"
    assert disabled.result == "succeeded"
    assert auth.get_auth_context(
        token_digest="a" * 64,
        now=NOW,
    ) is None
    assert auth.get_user_by_id(user_id).status == "disabled"
    assert [event.action for event in audit.events] == [
        "revoke_sessions",
        "disable_user",
    ]
    assert all(event.admin_id == admin_id for event in audit.events)
    assert all(event.target_user_id == user_id for event in audit.events)
    serialized = "".join(
        event.model_dump_json() for event in audit.events
    ).casefold()
    assert "user@example.com" not in serialized
    assert "consultation" not in serialized
