from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.agent.errors import CircuitTrippedError, RateLimitError
from app.agent.models import CaseContinuationResult, UsageInfo
from app.db.session import SessionStore
from app.limits.circuit import DailySpendCircuit
from app.limits.rate_limit import (
    DailyRateLimiter,
    hash_client_identifier,
)
from app.limits.usage import (
    ProviderUsageControls,
    UsagePricer,
    UsageTracker,
)
from app.providers.fake import FakeProvider
from tests.test_pipeline import create_confirmed_attachment, make_pipeline


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 6, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current


def make_store(tmp_path: Path, clock: Clock) -> SessionStore:
    store = SessionStore(
        tmp_path / "app.db",
        now=clock.now,
    )
    store.initialize()
    return store


def make_controls(
    store: SessionStore,
    clock: Clock,
    *,
    enabled: bool = True,
    rate_limit: int = 2,
    spend_limit: float = 1.0,
    input_price: float | None = 1.0,
    output_price: float | None = 2.0,
) -> ProviderUsageControls:
    return ProviderUsageControls(
        enabled=enabled,
        provider="deepseek",
        rate_limiter=DailyRateLimiter(
            store,
            limit=rate_limit,
            now=clock.now,
        ),
        circuit=DailySpendCircuit(
            store,
            provider="deepseek",
            limit_usd=spend_limit,
            now=clock.now,
        ),
        pricer=UsagePricer(
            input_per_million=input_price,
            output_per_million=output_price,
        ),
        tracker=UsageTracker(store),
    )


def test_client_identifier_is_hashed_deterministically() -> None:
    first = hash_client_identifier("127.0.0.1")
    second = hash_client_identifier("127.0.0.1")

    assert first == second
    assert len(first) == 64
    assert "127.0.0.1" not in first


def test_daily_rate_limit_is_atomic_at_boundary_and_resets(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = make_store(tmp_path, clock)
    limiter = DailyRateLimiter(store, limit=2, now=clock.now)

    assert limiter.check_and_increment("client") == 1
    assert limiter.check_and_increment("client") == 2
    with pytest.raises(RateLimitError):
        limiter.check_and_increment("client")

    clock.current += timedelta(days=1)
    assert limiter.check_and_increment("client") == 1


def test_usage_pricer_only_reports_explicitly_configured_cost() -> None:
    usage = UsageInfo(input_tokens=1_000_000, output_tokens=500_000)
    priced = UsagePricer(
        input_per_million=1.0,
        output_per_million=2.0,
    ).price(usage)
    unpriced = UsagePricer(
        input_per_million=None,
        output_per_million=None,
    ).price(usage)

    assert priced.estimated_cost_usd == pytest.approx(2.0)
    assert unpriced.estimated_cost_usd is None
    assert unpriced.total_tokens == 1_500_000


def test_daily_spend_circuit_trips_at_limit_and_recovers_next_day(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = make_store(tmp_path, clock)
    controls = make_controls(
        store,
        clock,
        spend_limit=0.001,
        input_price=1.0,
        output_price=0.0,
    )

    controls.before_call("client")
    priced = controls.after_call(
        "client",
        UsageInfo(input_tokens=1000),
    )

    assert priced.estimated_cost_usd == pytest.approx(0.001)
    assert controls.circuit.is_tripped() is True
    with pytest.raises(CircuitTrippedError):
        controls.before_call("client")

    clock.current += timedelta(days=1)
    assert controls.circuit.is_tripped() is False


def test_unpriced_usage_records_tokens_without_inventing_cost(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = make_store(tmp_path, clock)
    controls = make_controls(
        store,
        clock,
        input_price=None,
        output_price=None,
    )

    controls.before_call("client")
    usage = controls.after_call(
        "client",
        UsageInfo(input_tokens=10, output_tokens=4),
    )
    record = store.get_usage(
        day=date(2026, 8, 6),
        client_hash=hash_client_identifier("client"),
        provider="deepseek",
    )

    assert usage.estimated_cost_usd is None
    assert record is not None
    assert record.total_tokens == 14
    assert record.estimated_cost_usd is None
    assert controls.circuit.is_tripped() is False


def test_fake_provider_path_does_not_consume_limits(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = make_store(tmp_path, clock)
    controls = make_controls(
        store,
        clock,
        enabled=False,
        rate_limit=0,
    )

    controls.before_call("client")
    usage = controls.after_call("client", UsageInfo(input_tokens=10))

    assert usage.input_tokens == 10
    assert store.get_rate_limit(
        day=date(2026, 8, 6),
        client_hash=hash_client_identifier("client"),
    ) is None


def test_pipeline_checks_real_provider_limit_before_call(
    tmp_path: Path,
) -> None:
    class DeepSeekLikeFake(FakeProvider):
        name = "deepseek"
        model = "deepseek-test"

    clock = Clock()
    store = make_store(tmp_path, clock)
    controls = make_controls(store, clock, rate_limit=1)
    pipeline, _ = make_pipeline(
        tmp_path,
        provider=DeepSeekLikeFake(),
        usage_controls=controls,
    )

    first = asyncio.run(
        pipeline.consult(
            message="房东不退押金",
            client_identifier="127.0.0.1",
        )
    )
    assert first.usage.estimated_cost_usd == pytest.approx(0)
    attachment_id = create_confirmed_attachment(pipeline)

    with pytest.raises(RateLimitError):
        asyncio.run(
            pipeline.consult(
                message="房东不退押金",
                client_identifier="127.0.0.1",
                attachment_ids=[attachment_id],
            )
        )

    restored = pipeline.attachments.get(attachment_id)
    assert restored.status == "confirmed"
    assert restored.reservation_id is None
    assert restored.turn_id is None


def test_continuation_records_one_call_and_one_usage_entry(
    tmp_path: Path,
) -> None:
    class DeepSeekLikeFake(FakeProvider):
        name = "deepseek"
        model = "deepseek-test"

    clock = Clock()
    controls_store = make_store(tmp_path, clock)
    controls = make_controls(
        controls_store,
        clock,
        rate_limit=2,
    )
    provider = DeepSeekLikeFake(
        continuation_responses=[
            CaseContinuationResult(
                route="same_case",
                scenario_id="return_refused",
                answer="先保留拒绝处理的记录，再按原方案推进。",
                confidence=0.99,
                provider="deepseek",
                model="deepseek-test",
                usage=UsageInfo(input_tokens=7, output_tokens=5),
            )
        ]
    )
    pipeline, pipeline_store = make_pipeline(
        tmp_path,
        provider=provider,
        usage_controls=controls,
    )
    client_identifier = "127.0.0.1"

    first = asyncio.run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            client_identifier=client_identifier,
        )
    )
    second = asyncio.run(
        pipeline.consult(
            session_id=first.session_id,
            message="商家还是不配合怎么办",
            client_identifier=client_identifier,
        )
    )

    rate = controls_store.get_rate_limit(
        day=date(2026, 8, 6),
        client_hash=hash_client_identifier(client_identifier),
    )
    usage = controls_store.get_usage(
        day=date(2026, 8, 6),
        client_hash=hash_client_identifier(client_identifier),
        provider="deepseek",
    )
    assert second.turn_kind == "followup_answer"
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 1
    assert rate is not None
    assert rate.request_count == 2
    assert usage is not None
    assert usage.request_count == 2
    assert usage.total_tokens == 12

    with pytest.raises(RateLimitError):
        asyncio.run(
            pipeline.consult(
                session_id=first.session_id,
                message="再下一步呢",
                client_identifier=client_identifier,
            )
        )

    assert provider.continuation_calls == 1
    assert len(pipeline_store.list_turns(first.session_id)) == 2
