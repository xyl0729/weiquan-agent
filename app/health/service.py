from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.agent.models import UsageInfo
from app.providers.health import (
    ProviderCallResult,
    ProviderHealthState,
    ProviderHealthStatus,
    ProviderHealthStore,
    ProviderOutcome,
)


class ProviderHealthService:
    def __init__(
        self,
        store: ProviderHealthStore,
        *,
        window: timedelta = timedelta(minutes=30),
        sample_limit: int = 10,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("健康统计窗口必须大于零")
        if not 4 <= sample_limit <= 100:
            raise ValueError("健康样本上限不能小于四")
        self.store = store
        self.window = window
        self.sample_limit = sample_limit
        self._now = now or (lambda: datetime.now(UTC))

    def record(
        self,
        *,
        provider: str,
        model: str,
        outcome: ProviderOutcome,
        duration_ms: int,
        usage: UsageInfo | None = None,
        logical_call_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> ProviderHealthState:
        current = _utc(occurred_at or self._now())
        tokens = usage or UsageInfo()
        result = ProviderCallResult(
            logical_call_id=(
                UUID(logical_call_id)
                if logical_call_id is not None
                else uuid4()
            ),
            provider=provider,
            model=model,
            outcome=outcome,
            duration_ms=max(0, int(duration_ms)),
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            total_tokens=tokens.total_tokens,
            occurred_at=current,
        )
        self.store.add_result(result)
        state = self._calculate(provider, now=current)
        self.store.save_state(state)
        return state

    def status(
        self,
        provider: str,
        *,
        now: datetime | None = None,
    ) -> ProviderHealthState:
        current = _utc(now or self._now())
        state = self._calculate(provider, now=current)
        persisted = self.store.get_state(provider)
        if persisted != state:
            self.store.save_state(state)
        return state

    def _calculate(
        self,
        provider: str,
        *,
        now: datetime,
    ) -> ProviderHealthState:
        results = tuple(
            self.store.recent_results(
                provider,
                since=now - self.window,
                limit=self.sample_limit,
            )
        )
        previous = self.store.get_state(provider)
        if not results:
            return _state(
                provider,
                "unknown",
                (),
                now=now,
            )

        failures = sum(result.outcome != "success" for result in results)
        consecutive_successes = _consecutive(results, success=True)
        consecutive_failures = _consecutive(results, success=False)
        degraded_now = (
            consecutive_failures >= 3
            or (
                len(results) >= 4
                and failures / len(results) >= 0.5
            )
        )
        if degraded_now:
            status: ProviderHealthStatus = "degraded"
        elif (
            previous is not None
            and previous.status == "degraded"
            and consecutive_successes < 3
        ):
            status = "degraded"
        else:
            status = "healthy"
        return _state(provider, status, results, now=now)


def classify_provider_outcome(code: str) -> ProviderOutcome:
    return {
        "provider_timeout": "timeout",
        "provider_network": "network_error",
        "provider_rate_limited": "rate_limited",
        "provider_server_error": "server_error",
        "provider_invalid_output": "invalid_output",
        "provider_rejected": "rejected",
        "provider_configuration": "configuration_error",
    }.get(code, "provider_error")  # type: ignore[return-value]


def _state(
    provider: str,
    status: ProviderHealthStatus,
    results: tuple[ProviderCallResult, ...],
    *,
    now: datetime,
) -> ProviderHealthState:
    return ProviderHealthState(
        provider=provider,
        status=status,
        sample_count=len(results),
        failure_count=sum(
            result.outcome != "success" for result in results
        ),
        consecutive_successes=_consecutive(results, success=True),
        consecutive_failures=_consecutive(results, success=False),
        last_result_at=results[0].occurred_at if results else None,
        updated_at=now,
    )


def _consecutive(
    results: tuple[ProviderCallResult, ...],
    *,
    success: bool,
) -> int:
    count = 0
    for result in results:
        if (result.outcome == "success") is not success:
            break
        count += 1
    return count


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)
