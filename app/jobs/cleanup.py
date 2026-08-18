from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import Engine

from app.db.tables import (
    auth_sessions,
    auth_tokens,
    consultation_attachments,
    consultation_deletion_outbox,
    consultation_sessions,
    security_rate_limits,
    trial_identities,
    trial_ip_grants,
    users,
)


class CleanupStore(Protocol):
    def purge_expired_consultations(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...

    def purge_expired_attachments(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...

    def purge_expired_auth_sessions(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...

    def purge_expired_auth_tokens(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...

    def purge_expired_rate_limits(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...

    def expire_pending_users(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...

    def purge_expired_trial_ip_grants(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...

    def purge_expired_trial_identities(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...

    def purge_completed_deletion_outbox(
        self, *, cutoff: datetime, limit: int
    ) -> int: ...


class PendingDeletionProcessor(Protocol):
    def resume_pending(self, *, limit: int) -> int: ...


@dataclass(frozen=True, slots=True)
class CleanupReport:
    pending_deletions: int = 0
    consultations: int = 0
    attachments: int = 0
    auth_sessions: int = 0
    auth_tokens: int = 0
    rate_limits: int = 0
    pending_users: int = 0
    trial_ip_grants: int = 0
    trial_identities: int = 0
    deletion_outbox: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class CleanupJob:
    def __init__(
        self,
        *,
        store: CleanupStore,
        deletion_processor: PendingDeletionProcessor | None = None,
        pending_user_ttl: timedelta = timedelta(hours=24),
        deletion_outbox_retention: timedelta = timedelta(days=35),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if pending_user_ttl <= timedelta(0):
            raise ValueError("待验证账号保留期必须大于零")
        if deletion_outbox_retention <= timedelta(0):
            raise ValueError("删除清单保留期必须大于零")
        self.store = store
        self.deletion_processor = deletion_processor
        self.pending_user_ttl = pending_user_ttl
        self.deletion_outbox_retention = deletion_outbox_retention
        self._now = now or (lambda: datetime.now(UTC))

    def run_once(self, *, limit: int = 100) -> CleanupReport:
        bounded = _bounded_limit(limit)
        current = _utc(self._now())
        pending = (
            self.deletion_processor.resume_pending(limit=bounded)
            if self.deletion_processor is not None
            else 0
        )
        return CleanupReport(
            pending_deletions=pending,
            consultations=self.store.purge_expired_consultations(
                cutoff=current,
                limit=bounded,
            ),
            attachments=self.store.purge_expired_attachments(
                cutoff=current,
                limit=bounded,
            ),
            auth_sessions=self.store.purge_expired_auth_sessions(
                cutoff=current,
                limit=bounded,
            ),
            auth_tokens=self.store.purge_expired_auth_tokens(
                cutoff=current,
                limit=bounded,
            ),
            rate_limits=self.store.purge_expired_rate_limits(
                cutoff=current,
                limit=bounded,
            ),
            pending_users=self.store.expire_pending_users(
                cutoff=current - self.pending_user_ttl,
                limit=bounded,
            ),
            trial_ip_grants=(
                self.store.purge_expired_trial_ip_grants(
                    cutoff=current,
                    limit=bounded,
                )
            ),
            trial_identities=(
                self.store.purge_expired_trial_identities(
                    cutoff=current,
                    limit=bounded,
                )
            ),
            deletion_outbox=(
                self.store.purge_completed_deletion_outbox(
                    cutoff=current - self.deletion_outbox_retention,
                    limit=bounded,
                )
            ),
        )


class PostgresCleanupStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def purge_expired_consultations(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        targets = _targets(
            consultation_sessions.c.id,
            consultation_sessions.c.expires_at <= _utc(cutoff),
            consultation_sessions.c.deleted_at.is_(None),
            ordered_at=consultation_sessions.c.expires_at,
            limit=limit,
        )
        return self._delete(
            consultation_sessions,
            consultation_sessions.c.id.in_(targets),
        )

    def purge_expired_attachments(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        targets = _targets(
            consultation_attachments.c.id,
            consultation_attachments.c.session_id.is_(None),
            consultation_attachments.c.expires_at <= _utc(cutoff),
            ordered_at=consultation_attachments.c.expires_at,
            limit=limit,
        )
        return self._delete(
            consultation_attachments,
            consultation_attachments.c.id.in_(targets),
        )

    def purge_expired_auth_sessions(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        targets = _targets(
            auth_sessions.c.id,
            auth_sessions.c.expires_at <= _utc(cutoff),
            ordered_at=auth_sessions.c.expires_at,
            limit=limit,
        )
        return self._delete(
            auth_sessions,
            auth_sessions.c.id.in_(targets),
        )

    def purge_expired_auth_tokens(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        current = _utc(cutoff)
        targets = _targets(
            auth_tokens.c.id,
            or_(
                auth_tokens.c.expires_at <= current,
                auth_tokens.c.consumed_at <= current,
                auth_tokens.c.invalidated_at <= current,
            ),
            ordered_at=auth_tokens.c.expires_at,
            limit=limit,
        )
        return self._delete(auth_tokens, auth_tokens.c.id.in_(targets))

    def purge_expired_rate_limits(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        targets = _targets(
            security_rate_limits.c.id,
            security_rate_limits.c.expires_at <= _utc(cutoff),
            ordered_at=security_rate_limits.c.expires_at,
            limit=limit,
        )
        return self._delete(
            security_rate_limits,
            security_rate_limits.c.id.in_(targets),
        )

    def expire_pending_users(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        current = _utc(cutoff)
        targets = _targets(
            users.c.id,
            users.c.status == "pending_verification",
            users.c.created_at <= current,
            ordered_at=users.c.created_at,
            limit=limit,
        )
        with self.engine.begin() as connection:
            result = connection.execute(
                update(users)
                .where(
                    users.c.id.in_(targets),
                    users.c.status == "pending_verification",
                )
                .values(
                    email_normalized=None,
                    password_hash=None,
                    status="deleted",
                    deleted_at=current,
                    updated_at=current,
                )
            )
        return max(int(result.rowcount or 0), 0)

    def purge_expired_trial_ip_grants(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        targets = _targets(
            trial_ip_grants.c.id,
            trial_ip_grants.c.expires_at <= _utc(cutoff),
            ordered_at=trial_ip_grants.c.expires_at,
            limit=limit,
        )
        return self._delete(
            trial_ip_grants,
            trial_ip_grants.c.id.in_(targets),
        )

    def purge_expired_trial_identities(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        targets = _targets(
            trial_identities.c.id,
            trial_identities.c.expires_at <= _utc(cutoff),
            ordered_at=trial_identities.c.expires_at,
            limit=limit,
        )
        return self._delete(
            trial_identities,
            trial_identities.c.id.in_(targets),
        )

    def purge_completed_deletion_outbox(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        targets = _targets(
            consultation_deletion_outbox.c.session_id,
            consultation_deletion_outbox.c.completed_at.is_not(None),
            consultation_deletion_outbox.c.completed_at <= _utc(cutoff),
            ordered_at=consultation_deletion_outbox.c.completed_at,
            limit=limit,
        )
        return self._delete(
            consultation_deletion_outbox,
            consultation_deletion_outbox.c.session_id.in_(targets),
        )

    def _delete(self, table: object, condition: object) -> int:
        with self.engine.begin() as connection:
            result = connection.execute(delete(table).where(condition))
        return max(int(result.rowcount or 0), 0)


def _targets(
    identifier: object,
    *conditions: object,
    ordered_at: object,
    limit: int,
) -> object:
    bounded = _bounded_limit(limit)
    return (
        select(identifier)
        .where(*conditions)
        .order_by(ordered_at, identifier)
        .limit(bounded)
    )


def _bounded_limit(limit: int) -> int:
    normalized = int(limit)
    if not 1 <= normalized <= 1000:
        raise ValueError("limit 必须在 1 到 1000 之间")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("清理时间必须包含时区")
    return value.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行一次有界生产数据清理",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="每类资源本次最多处理数量（1-1000）",
    )
    arguments = parser.parse_args(argv)
    limit = _bounded_limit(arguments.limit)

    from app.config import get_settings
    from app.db.engine import create_database_engine
    from app.db.postgres import PostgresApplicationStore
    from app.deps import build_deletion_service

    settings = get_settings()
    if settings.deployment_mode != "production":
        parser.error("清理 CLI 仅允许在 production 模式运行")
    engine = create_database_engine(settings)
    try:
        repository = PostgresApplicationStore(
            engine,
            retention_days=settings.session_retention_days,
            attachment_draft_ttl_seconds=(
                settings.attachment_draft_ttl_seconds
            ),
        )
        job = CleanupJob(
            store=PostgresCleanupStore(engine),
            deletion_processor=build_deletion_service(
                settings,
                repository,
            ),
            pending_user_ttl=timedelta(
                hours=settings.pending_registration_ttl_hours
            ),
        )
        print(
            json.dumps(
                job.run_once(limit=limit).to_dict(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
