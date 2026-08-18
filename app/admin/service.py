from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import insert
from sqlalchemy.engine import Engine

from app.admin.models import (
    AdminAccountDiagnostics,
    AdminAction,
    AdminActionResult,
    AdminAuditEvent,
    AdminDiagnostics,
    AdminProviderDiagnostics,
)
from app.auth.store import AuthStore
from app.health.service import ProviderHealthService
from app.limits.reservations import QuotaService


class AdminAuditStore(Protocol):
    def add(self, event: AdminAuditEvent) -> None: ...


class InMemoryAdminAuditStore:
    def __init__(self) -> None:
        self.events: list[AdminAuditEvent] = []

    def add(self, event: AdminAuditEvent) -> None:
        if any(existing.id == event.id for existing in self.events):
            return
        self.events.append(event)


class PostgresAdminAuditStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def add(self, event: AdminAuditEvent) -> None:
        from app.db.tables import admin_audit_events

        values = event.model_dump(mode="python")
        values["id"] = str(values["id"])
        with self.engine.begin() as connection:
            connection.execute(insert(admin_audit_events).values(**values))


class AdminService:
    def __init__(
        self,
        *,
        auth_store: AuthStore,
        quota_service: QuotaService,
        provider_health: ProviderHealthService,
        audit_store: AdminAuditStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.auth_store = auth_store
        self.quota_service = quota_service
        self.provider_health = provider_health
        self.audit_store = audit_store
        self._now = now or (lambda: datetime.now(UTC))

    def diagnostics(self) -> AdminDiagnostics:
        accounts = [
            AdminAccountDiagnostics(
                user_id=user.id,
                status=user.status,
                email_verified=user.verified_at is not None,
                created_at=user.created_at,
                quota=self.quota_service.registered_status(user.id),
            )
            for user in self.auth_store.list_users()
        ]
        current = _utc(self._now())
        state = self.provider_health.status(
            "deepseek",
            now=current,
        )
        results = tuple(
            self.provider_health.store.recent_results(
                "deepseek",
                since=current - self.provider_health.window,
                limit=self.provider_health.sample_limit,
            )
        )
        error_categories = sorted(
            {
                result.outcome
                for result in results
                if result.outcome != "success"
            }
        )
        provider = AdminProviderDiagnostics(
            provider="deepseek",
            status=state.status,
            sample_count=len(results),
            success_count=sum(
                result.outcome == "success" for result in results
            ),
            error_categories=error_categories,
            last_result_at=(
                results[0].occurred_at if results else None
            ),
        )
        return AdminDiagnostics(
            accounts=accounts,
            provider=provider,
        )

    def revoke_user_sessions(
        self,
        *,
        admin_id: str,
        target_user_id: str,
    ) -> AdminActionResult:
        now = _utc(self._now())
        exists = self.auth_store.get_user_by_id(target_user_id) is not None
        if exists:
            self.auth_store.revoke_user_sessions(
                user_id=target_user_id,
                now=now,
            )
        return self._record_action(
            admin_id=admin_id,
            target_user_id=target_user_id,
            action="revoke_sessions",
            succeeded=exists,
            occurred_at=now,
        )

    def disable_user(
        self,
        *,
        admin_id: str,
        target_user_id: str,
    ) -> AdminActionResult:
        now = _utc(self._now())
        disabled = self.auth_store.disable_user(
            user_id=target_user_id,
            now=now,
        )
        return self._record_action(
            admin_id=admin_id,
            target_user_id=target_user_id,
            action="disable_user",
            succeeded=disabled is not None,
            occurred_at=now,
        )

    def _record_action(
        self,
        *,
        admin_id: str,
        target_user_id: str,
        action: AdminAction,
        succeeded: bool,
        occurred_at: datetime,
    ) -> AdminActionResult:
        result = "succeeded" if succeeded else "not_found"
        self.audit_store.add(
            AdminAuditEvent(
                admin_id=admin_id,
                target_user_id=target_user_id,
                action=action,
                occurred_at=occurred_at,
                result=result,
            )
        )
        return AdminActionResult(
            action=action,
            target_user_id=target_user_id,
            result=result,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)

