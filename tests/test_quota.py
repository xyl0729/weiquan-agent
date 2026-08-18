from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.limits.quota import QuotaExceededError
from app.limits.reservations import (
    InMemoryQuotaStore,
    QuotaService,
)


NOW = datetime(2026, 8, 10, 15, 59, tzinfo=UTC)
TRIAL_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"


def _service(
    *,
    now: datetime = NOW,
    store: InMemoryQuotaStore | None = None,
) -> QuotaService:
    return QuotaService(
        store or InMemoryQuotaStore(),
        now=lambda: now,
    )


def test_trial_five_calls_then_sixth_is_rejected() -> None:
    service = _service()

    for _ in range(5):
        reservation = service.reserve_trial(
            identity_id=TRIAL_ID,
            logical_call_id=str(uuid4()),
        )
        assert service.succeed(reservation.id).status == "succeeded"

    status = service.trial_status(TRIAL_ID)
    assert status.remaining_total == 0
    with pytest.raises(QuotaExceededError) as caught:
        service.reserve_trial(
            identity_id=TRIAL_ID,
            logical_call_id=str(uuid4()),
        )
    assert caught.value.code == "trial_quota_exceeded"


def test_global_trial_day_rejects_the_51st_reservation() -> None:
    service = _service()

    for index in range(50):
        identity_id = f"00000000-0000-4000-8000-{index:012d}"
        service.succeed(
            service.reserve_trial(
                identity_id=identity_id,
                logical_call_id=str(uuid4()),
            ).id
        )

    fresh_identity = "99999999-9999-4999-8999-999999999999"
    with pytest.raises(QuotaExceededError) as caught:
        service.reserve_trial(
            identity_id=fresh_identity,
            logical_call_id=str(uuid4()),
        )
    assert caught.value.code == "trial_daily_capacity_exceeded"
    assert service.trial_status(fresh_identity).remaining_total == 5


def test_registered_day_and_month_use_beijing_boundaries() -> None:
    store = InMemoryQuotaStore()
    before_midnight = _service(now=NOW, store=store)
    for _ in range(10):
        before_midnight.succeed(
            before_midnight.reserve_registered(
                user_id=USER_ID,
                logical_call_id=str(uuid4()),
            ).id
        )

    status = before_midnight.registered_status(USER_ID)
    assert status.remaining_daily == 0
    assert status.remaining_monthly == 40
    assert status.day_resets_at == datetime(
        2026, 8, 10, 16, 0, tzinfo=UTC
    )

    after_midnight = _service(
        now=NOW + timedelta(minutes=2),
        store=store,
    )
    assert after_midnight.registered_status(USER_ID).remaining_daily == 10
    after_midnight.succeed(
        after_midnight.reserve_registered(
            user_id=USER_ID,
            logical_call_id=str(uuid4()),
        ).id
    )
    assert after_midnight.registered_status(USER_ID).remaining_monthly == 39

    september = _service(
        now=datetime(2026, 8, 31, 16, 0, tzinfo=UTC),
        store=store,
    )
    september_status = september.registered_status(USER_ID)
    assert september_status.remaining_daily == 10
    assert september_status.remaining_monthly == 50


def test_reserve_success_and_refund_are_idempotent() -> None:
    service = _service()
    logical_call_id = str(uuid4())

    first = service.reserve_registered(
        user_id=USER_ID,
        logical_call_id=logical_call_id,
    )
    duplicate = service.reserve_registered(
        user_id=USER_ID,
        logical_call_id=logical_call_id,
    )
    assert duplicate.id == first.id
    assert service.registered_status(USER_ID).remaining_daily == 9

    refunded = service.refund(first.id)
    refunded_again = service.refund(first.id)
    assert refunded.status == refunded_again.status == "refunded"
    assert service.registered_status(USER_ID).remaining_daily == 10

    succeeded_after_refund = service.succeed(first.id)
    assert succeeded_after_refund.status == "refunded"
    assert service.registered_status(USER_ID).remaining_daily == 10


def test_stale_reservations_are_refunded_but_successes_are_not() -> None:
    store = InMemoryQuotaStore()
    service = _service(store=store)
    stale = service.reserve_registered(
        user_id=USER_ID,
        logical_call_id=str(uuid4()),
    )
    succeeded = service.reserve_registered(
        user_id=USER_ID,
        logical_call_id=str(uuid4()),
    )
    service.succeed(succeeded.id)

    recovered = service.recover_stale(
        now=NOW + timedelta(minutes=5, seconds=1)
    )

    assert recovered == 1
    assert store.get_reservation(stale.id).status == "refunded"
    assert store.get_reservation(succeeded.id).status == "succeeded"
    assert service.registered_status(USER_ID).remaining_daily == 9


def test_concurrent_trial_reservations_do_not_exceed_limit() -> None:
    service = _service()

    def reserve(index: int) -> str:
        try:
            return service.reserve_trial(
                identity_id=TRIAL_ID,
                logical_call_id=f"10000000-0000-4000-8000-{index:012d}",
            ).status
        except QuotaExceededError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=12) as executor:
        statuses = list(executor.map(reserve, range(20)))

    assert statuses.count("reserved") == 5
    assert statuses.count("rejected") == 15
    assert service.trial_status(TRIAL_ID).remaining_total == 0
