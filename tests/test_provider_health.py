from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.health.service import ProviderHealthService
from app.providers.health import InMemoryProviderHealthStore


BASE = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)


def _record(
    service: ProviderHealthService,
    *,
    outcome: str,
    minute: int,
) -> None:
    service.record(
        provider="deepseek",
        model="deepseek-chat",
        outcome=outcome,  # type: ignore[arg-type]
        duration_ms=25,
        occurred_at=BASE + timedelta(minutes=minute),
    )


def test_provider_stays_unknown_without_real_results() -> None:
    service = ProviderHealthService(
        InMemoryProviderHealthStore(),
        now=lambda: BASE,
    )

    state = service.status("deepseek")

    assert state.status == "unknown"
    assert state.sample_count == 0
    assert state.last_result_at is None


def test_failure_ratio_degrades_and_three_successes_recover() -> None:
    store = InMemoryProviderHealthStore()
    service = ProviderHealthService(store)
    for minute, outcome in enumerate(
        ("success", "timeout", "success", "server_error")
    ):
        _record(service, outcome=outcome, minute=minute)

    assert service.status(
        "deepseek",
        now=BASE + timedelta(minutes=3),
    ).status == "degraded"

    for minute in range(4, 7):
        _record(service, outcome="success", minute=minute)
    recovered = service.status(
        "deepseek",
        now=BASE + timedelta(minutes=6),
    )
    assert recovered.status == "healthy"
    assert recovered.consecutive_successes == 3


def test_three_consecutive_failures_degrade_without_four_samples() -> None:
    service = ProviderHealthService(InMemoryProviderHealthStore())
    for minute in range(3):
        _record(service, outcome="network_error", minute=minute)

    state = service.status(
        "deepseek",
        now=BASE + timedelta(minutes=2),
    )
    assert state.status == "degraded"
    assert state.sample_count == 3
    assert state.consecutive_failures == 3


def test_old_results_expire_back_to_unknown() -> None:
    service = ProviderHealthService(InMemoryProviderHealthStore())
    _record(service, outcome="success", minute=0)

    state = service.status(
        "deepseek",
        now=BASE + timedelta(minutes=31),
    )
    assert state.status == "unknown"
    assert state.sample_count == 0


def test_result_record_contains_only_content_free_metadata() -> None:
    store = InMemoryProviderHealthStore()
    service = ProviderHealthService(store)
    _record(service, outcome="success", minute=0)

    result = store.recent_results(
        "deepseek",
        since=BASE,
        limit=10,
    )[0]
    assert set(result.model_dump()) == {
        "id",
        "logical_call_id",
        "provider",
        "model",
        "outcome",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "occurred_at",
    }
