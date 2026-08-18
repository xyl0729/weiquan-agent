from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app.limits.quota import (
    QuotaBucketSpec,
    QuotaExceededError,
    QuotaKind,
    QuotaReservation,
    RegisteredQuotaStatus,
    TrialQuotaStatus,
)


BEIJING = ZoneInfo("Asia/Shanghai")
STALE_AFTER = timedelta(minutes=5)


class QuotaStore(Protocol):
    def reserve(
        self,
        *,
        kind: QuotaKind,
        subject_id: str,
        logical_call_id: str,
        buckets: Sequence[QuotaBucketSpec],
        now: datetime,
    ) -> QuotaReservation: ...

    def transition(
        self,
        reservation_id: str,
        *,
        target: str,
        now: datetime,
    ) -> QuotaReservation: ...

    def get_reservation(
        self,
        reservation_id: str,
    ) -> QuotaReservation: ...

    def bucket_count(self, spec: QuotaBucketSpec) -> int: ...

    def recover_stale(
        self,
        *,
        cutoff: datetime,
        now: datetime,
        limit: int = 100,
    ) -> int: ...


class InMemoryQuotaStore:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, str], int] = {}
        self._reservations: dict[str, QuotaReservation] = {}
        self._logical: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()

    def reserve(
        self,
        *,
        kind: QuotaKind,
        subject_id: str,
        logical_call_id: str,
        buckets: Sequence[QuotaBucketSpec],
        now: datetime,
    ) -> QuotaReservation:
        current = _utc(now)
        logical_key = (kind, subject_id, logical_call_id)
        ordered = tuple(sorted(buckets, key=lambda item: item.key))
        with self._lock:
            existing_id = self._logical.get(logical_key)
            if existing_id is not None:
                return self._reservations[existing_id]
            for spec in ordered:
                if self._counts.get(spec.key, 0) >= spec.limit:
                    raise QuotaExceededError(spec.exceeded_code)
            reservation = QuotaReservation(
                id=str(uuid4()),
                kind=kind,
                subject_id=subject_id,
                logical_call_id=logical_call_id,
                status="reserved",
                bucket_keys=tuple(spec.key for spec in ordered),
                created_at=current,
                updated_at=current,
            )
            self._reservations[reservation.id] = reservation
            self._logical[logical_key] = reservation.id
            for spec in ordered:
                self._counts[spec.key] = self._counts.get(spec.key, 0) + 1
            return reservation

    def transition(
        self,
        reservation_id: str,
        *,
        target: str,
        now: datetime,
    ) -> QuotaReservation:
        if target not in {"succeeded", "refunded"}:
            raise ValueError("配额预留目标状态无效")
        current = _utc(now)
        with self._lock:
            existing = self._reservations[reservation_id]
            if existing.status != "reserved":
                return existing
            if target == "refunded":
                for key in existing.bucket_keys:
                    self._counts[key] = max(
                        0,
                        self._counts.get(key, 0) - 1,
                    )
            changed = existing.model_copy(
                update={"status": target, "updated_at": current}
            )
            self._reservations[reservation_id] = changed
            return changed

    def get_reservation(
        self,
        reservation_id: str,
    ) -> QuotaReservation:
        with self._lock:
            return self._reservations[reservation_id]

    def bucket_count(self, spec: QuotaBucketSpec) -> int:
        with self._lock:
            return self._counts.get(spec.key, 0)

    def recover_stale(
        self,
        *,
        cutoff: datetime,
        now: datetime,
        limit: int = 100,
    ) -> int:
        stale_cutoff = _utc(cutoff)
        with self._lock:
            stale_ids = [
                reservation.id
                for reservation in self._reservations.values()
                if (
                    reservation.status == "reserved"
                    and reservation.created_at < stale_cutoff
                )
            ][:limit]
            for reservation_id in stale_ids:
                self.transition(
                    reservation_id,
                    target="refunded",
                    now=now,
                )
            return len(stale_ids)


class PostgresQuotaStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def reserve(
        self,
        *,
        kind: QuotaKind,
        subject_id: str,
        logical_call_id: str,
        buckets: Sequence[QuotaBucketSpec],
        now: datetime,
    ) -> QuotaReservation:
        from app.db.tables import (
            quota_buckets,
            quota_reservation_buckets,
            quota_reservations,
        )

        current = _utc(now)
        ordered = tuple(sorted(buckets, key=lambda item: item.key))
        reservation_id = str(uuid4())
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(quota_reservations)
                .where(
                    quota_reservations.c.kind == kind,
                    quota_reservations.c.subject_id == subject_id,
                    quota_reservations.c.logical_call_id
                    == logical_call_id,
                )
                .with_for_update()
            ).mappings().first()
            if existing is not None:
                return self._reservation_from_row(
                    connection,
                    existing,
                )

            for spec in ordered:
                connection.execute(
                    pg_insert(quota_buckets)
                    .values(
                        id=str(uuid4()),
                        bucket_type=spec.bucket_type,
                        subject_key=spec.subject_key,
                        period_key=spec.period_key,
                        used_count=0,
                        limit_count=spec.limit,
                        resets_at=spec.resets_at,
                        updated_at=current,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            quota_buckets.c.bucket_type,
                            quota_buckets.c.subject_key,
                            quota_buckets.c.period_key,
                        ]
                    )
                )

            predicates = [
                and_(
                    quota_buckets.c.bucket_type == spec.bucket_type,
                    quota_buckets.c.subject_key == spec.subject_key,
                    quota_buckets.c.period_key == spec.period_key,
                )
                for spec in ordered
            ]
            locked_rows = connection.execute(
                select(quota_buckets)
                .where(_or_all(predicates))
                .order_by(
                    quota_buckets.c.bucket_type,
                    quota_buckets.c.subject_key,
                    quota_buckets.c.period_key,
                )
                .with_for_update()
            ).mappings().all()
            by_key = {
                (
                    str(row["bucket_type"]),
                    str(row["subject_key"]),
                    str(row["period_key"]),
                ): row
                for row in locked_rows
            }
            for spec in ordered:
                row = by_key[spec.key]
                if int(row["used_count"]) >= spec.limit:
                    raise QuotaExceededError(spec.exceeded_code)

            inserted = connection.execute(
                pg_insert(quota_reservations)
                .values(
                    id=reservation_id,
                    kind=kind,
                    subject_id=subject_id,
                    logical_call_id=logical_call_id,
                    status="reserved",
                    created_at=current,
                    updated_at=current,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        quota_reservations.c.kind,
                        quota_reservations.c.subject_id,
                        quota_reservations.c.logical_call_id,
                    ]
                )
                .returning(quota_reservations.c.id)
            ).scalar_one_or_none()
            if inserted is None:
                duplicate = connection.execute(
                    select(quota_reservations).where(
                        quota_reservations.c.kind == kind,
                        quota_reservations.c.subject_id == subject_id,
                        quota_reservations.c.logical_call_id
                        == logical_call_id,
                    )
                ).mappings().one()
                return self._reservation_from_row(
                    connection,
                    duplicate,
                )

            for spec in ordered:
                row = by_key[spec.key]
                connection.execute(
                    update(quota_buckets)
                    .where(quota_buckets.c.id == row["id"])
                    .values(
                        used_count=quota_buckets.c.used_count + 1,
                        limit_count=spec.limit,
                        resets_at=spec.resets_at,
                        updated_at=current,
                    )
                )
                connection.execute(
                    insert(quota_reservation_buckets).values(
                        reservation_id=reservation_id,
                        bucket_id=row["id"],
                    )
                )
            return QuotaReservation(
                id=reservation_id,
                kind=kind,
                subject_id=subject_id,
                logical_call_id=logical_call_id,
                status="reserved",
                bucket_keys=tuple(spec.key for spec in ordered),
                created_at=current,
                updated_at=current,
            )

    def transition(
        self,
        reservation_id: str,
        *,
        target: str,
        now: datetime,
    ) -> QuotaReservation:
        if target not in {"succeeded", "refunded"}:
            raise ValueError("配额预留目标状态无效")
        from app.db.tables import (
            quota_buckets,
            quota_reservation_buckets,
            quota_reservations,
        )

        current = _utc(now)
        with self._engine.begin() as connection:
            row = connection.execute(
                select(quota_reservations)
                .where(quota_reservations.c.id == reservation_id)
                .with_for_update()
            ).mappings().one()
            if row["status"] != "reserved":
                return self._reservation_from_row(connection, row)
            linked = connection.execute(
                select(quota_buckets)
                .join(
                    quota_reservation_buckets,
                    quota_reservation_buckets.c.bucket_id
                    == quota_buckets.c.id,
                )
                .where(
                    quota_reservation_buckets.c.reservation_id
                    == reservation_id
                )
                .order_by(
                    quota_buckets.c.bucket_type,
                    quota_buckets.c.subject_key,
                    quota_buckets.c.period_key,
                )
                .with_for_update()
            ).mappings().all()
            if target == "refunded":
                for bucket in linked:
                    connection.execute(
                        update(quota_buckets)
                        .where(quota_buckets.c.id == bucket["id"])
                        .values(
                            used_count=quota_buckets.c.used_count - 1,
                            updated_at=current,
                        )
                    )
            connection.execute(
                update(quota_reservations)
                .where(quota_reservations.c.id == reservation_id)
                .values(status=target, updated_at=current)
            )
            values = dict(row)
            values["status"] = target
            values["updated_at"] = current
            return self._reservation_from_values(values, linked)

    def get_reservation(
        self,
        reservation_id: str,
    ) -> QuotaReservation:
        from app.db.tables import quota_reservations

        with self._engine.connect() as connection:
            row = connection.execute(
                select(quota_reservations).where(
                    quota_reservations.c.id == reservation_id
                )
            ).mappings().one()
            return self._reservation_from_row(connection, row)

    def bucket_count(self, spec: QuotaBucketSpec) -> int:
        from app.db.tables import quota_buckets

        with self._engine.connect() as connection:
            value = connection.execute(
                select(quota_buckets.c.used_count).where(
                    quota_buckets.c.bucket_type == spec.bucket_type,
                    quota_buckets.c.subject_key == spec.subject_key,
                    quota_buckets.c.period_key == spec.period_key,
                )
            ).scalar_one_or_none()
        return int(value or 0)

    def recover_stale(
        self,
        *,
        cutoff: datetime,
        now: datetime,
        limit: int = 100,
    ) -> int:
        from app.db.tables import quota_reservations

        with self._engine.connect() as connection:
            ids = connection.execute(
                select(quota_reservations.c.id)
                .where(
                    quota_reservations.c.status == "reserved",
                    quota_reservations.c.created_at < _utc(cutoff),
                )
                .order_by(quota_reservations.c.created_at)
                .limit(limit)
            ).scalars().all()
        recovered = 0
        for reservation_id in ids:
            changed = self.transition(
                str(reservation_id),
                target="refunded",
                now=now,
            )
            if changed.status == "refunded":
                recovered += 1
        return recovered

    @staticmethod
    def _reservation_from_row(
        connection: object,
        row: object,
    ) -> QuotaReservation:
        from app.db.tables import (
            quota_buckets,
            quota_reservation_buckets,
        )

        values = dict(row)  # type: ignore[arg-type]
        linked = connection.execute(  # type: ignore[union-attr]
            select(quota_buckets)
            .join(
                quota_reservation_buckets,
                quota_reservation_buckets.c.bucket_id
                == quota_buckets.c.id,
            )
            .where(
                quota_reservation_buckets.c.reservation_id
                == values["id"]
            )
            .order_by(
                quota_buckets.c.bucket_type,
                quota_buckets.c.subject_key,
                quota_buckets.c.period_key,
            )
        ).mappings().all()
        return PostgresQuotaStore._reservation_from_values(
            values,
            linked,
        )

    @staticmethod
    def _reservation_from_values(
        values: dict[str, object],
        linked: Sequence[object],
    ) -> QuotaReservation:
        return QuotaReservation(
            id=str(values["id"]),
            kind=str(values["kind"]),
            subject_id=str(values["subject_id"]),
            logical_call_id=str(values["logical_call_id"]),
            status=str(values["status"]),
            bucket_keys=tuple(
                (
                    str(dict(row)["bucket_type"]),
                    str(dict(row)["subject_key"]),
                    str(dict(row)["period_key"]),
                )
                for row in linked
            ),
            created_at=values["created_at"],
            updated_at=values["updated_at"],
        )


class QuotaService:
    def __init__(
        self,
        store: QuotaStore,
        *,
        trial_total_limit: int = 5,
        trial_global_daily_limit: int = 50,
        registered_daily_limit: int = 10,
        registered_monthly_limit: int = 50,
        stale_after: timedelta = STALE_AFTER,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        limits = (
            trial_total_limit,
            trial_global_daily_limit,
            registered_daily_limit,
            registered_monthly_limit,
        )
        if any(limit < 1 for limit in limits):
            raise ValueError("配额上限必须大于零")
        if stale_after <= timedelta(0):
            raise ValueError("孤立预留恢复时间必须大于零")
        self.store = store
        self.trial_total_limit = trial_total_limit
        self.trial_global_daily_limit = trial_global_daily_limit
        self.registered_daily_limit = registered_daily_limit
        self.registered_monthly_limit = registered_monthly_limit
        self.stale_after = stale_after
        self._now = now or (lambda: datetime.now(UTC))

    def reserve_trial(
        self,
        *,
        identity_id: str,
        logical_call_id: str,
    ) -> QuotaReservation:
        current = _utc(self._now())
        return self.store.reserve(
            kind="trial",
            subject_id=identity_id,
            logical_call_id=logical_call_id,
            buckets=self._trial_specs(identity_id, current),
            now=current,
        )

    def reserve_registered(
        self,
        *,
        user_id: str,
        logical_call_id: str,
    ) -> QuotaReservation:
        current = _utc(self._now())
        return self.store.reserve(
            kind="registered",
            subject_id=user_id,
            logical_call_id=logical_call_id,
            buckets=self._registered_specs(user_id, current),
            now=current,
        )

    def succeed(self, reservation_id: str) -> QuotaReservation:
        return self.store.transition(
            reservation_id,
            target="succeeded",
            now=_utc(self._now()),
        )

    def refund(self, reservation_id: str) -> QuotaReservation:
        return self.store.transition(
            reservation_id,
            target="refunded",
            now=_utc(self._now()),
        )

    def recover_stale(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        current = _utc(now or self._now())
        return self.store.recover_stale(
            cutoff=current - self.stale_after,
            now=current,
            limit=limit,
        )

    def trial_status(self, identity_id: str) -> TrialQuotaStatus:
        current = _utc(self._now())
        lifetime = self._trial_specs(identity_id, current)[0]
        return TrialQuotaStatus(
            remaining_total=max(
                0,
                lifetime.limit - self.store.bucket_count(lifetime),
            )
        )

    def registered_status(
        self,
        user_id: str,
    ) -> RegisteredQuotaStatus:
        current = _utc(self._now())
        daily, monthly = self._registered_specs(user_id, current)
        return RegisteredQuotaStatus(
            remaining_daily=max(
                0,
                daily.limit - self.store.bucket_count(daily),
            ),
            remaining_monthly=max(
                0,
                monthly.limit - self.store.bucket_count(monthly),
            ),
            day_resets_at=daily.resets_at,
            month_resets_at=monthly.resets_at,
        )

    def _trial_specs(
        self,
        identity_id: str,
        now: datetime,
    ) -> tuple[QuotaBucketSpec, QuotaBucketSpec]:
        day_key, day_reset = _beijing_day(now)
        return (
            QuotaBucketSpec(
                bucket_type="trial_total",
                subject_key=identity_id,
                period_key="lifetime",
                limit=self.trial_total_limit,
                exceeded_code="trial_quota_exceeded",
            ),
            QuotaBucketSpec(
                bucket_type="trial_global_day",
                subject_key="global",
                period_key=day_key,
                limit=self.trial_global_daily_limit,
                exceeded_code="trial_daily_capacity_exceeded",
                resets_at=day_reset,
            ),
        )

    def _registered_specs(
        self,
        user_id: str,
        now: datetime,
    ) -> tuple[QuotaBucketSpec, QuotaBucketSpec]:
        day_key, day_reset = _beijing_day(now)
        month_key, month_reset = _beijing_month(now)
        return (
            QuotaBucketSpec(
                bucket_type="registered_day",
                subject_key=user_id,
                period_key=day_key,
                limit=self.registered_daily_limit,
                exceeded_code="registered_daily_quota_exceeded",
                resets_at=day_reset,
            ),
            QuotaBucketSpec(
                bucket_type="registered_month",
                subject_key=user_id,
                period_key=month_key,
                limit=self.registered_monthly_limit,
                exceeded_code="registered_monthly_quota_exceeded",
                resets_at=month_reset,
            ),
        )


class QuotaCallController:
    def __init__(
        self,
        service: QuotaService,
        *,
        kind: QuotaKind,
        subject_id: str,
        logical_call_id: str,
    ) -> None:
        self._service = service
        self._kind = kind
        self._subject_id = subject_id
        self._logical_call_id = logical_call_id
        self._reservation: QuotaReservation | None = None

    @property
    def reservation_id(self) -> str | None:
        return (
            self._reservation.id
            if self._reservation is not None
            else None
        )

    def reserve(self) -> QuotaReservation:
        if self._reservation is not None:
            return self._reservation
        if self._kind == "trial":
            self._reservation = self._service.reserve_trial(
                identity_id=self._subject_id,
                logical_call_id=self._logical_call_id,
            )
        else:
            self._reservation = self._service.reserve_registered(
                user_id=self._subject_id,
                logical_call_id=self._logical_call_id,
            )
        return self._reservation

    def succeed(self) -> None:
        if (
            self._reservation is not None
            and self._reservation.status == "reserved"
        ):
            self._reservation = self._service.succeed(
                self._reservation.id
            )

    def refund(self) -> None:
        if (
            self._reservation is not None
            and self._reservation.status == "reserved"
        ):
            self._reservation = self._service.refund(
                self._reservation.id
            )


def _beijing_day(now: datetime) -> tuple[str, datetime]:
    local = _utc(now).astimezone(BEIJING)
    next_day = (local + timedelta(days=1)).date()
    reset = datetime.combine(
        next_day,
        datetime.min.time(),
        tzinfo=BEIJING,
    ).astimezone(UTC)
    return local.date().isoformat(), reset


def _beijing_month(now: datetime) -> tuple[str, datetime]:
    local = _utc(now).astimezone(BEIJING)
    if local.month == 12:
        next_year, next_month = local.year + 1, 1
    else:
        next_year, next_month = local.year, local.month + 1
    reset = datetime(
        next_year,
        next_month,
        1,
        tzinfo=BEIJING,
    ).astimezone(UTC)
    return f"{local.year:04d}-{local.month:02d}", reset


def _or_all(predicates: Sequence[object]) -> object:
    from sqlalchemy import or_

    if not predicates:
        raise ValueError("配额预留至少需要一个桶")
    return or_(*predicates)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)
