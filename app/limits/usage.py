from __future__ import annotations

from app.agent.models import UsageInfo
from app.db.models import UsageDailyRecord
from app.db.session import SessionStore
from app.limits.circuit import DailySpendCircuit
from app.limits.rate_limit import (
    DailyRateLimiter,
    hash_client_identifier,
)


class UsagePricer:
    def __init__(
        self,
        *,
        input_per_million: float | None,
        output_per_million: float | None,
    ) -> None:
        if (input_per_million is None) != (output_per_million is None):
            raise ValueError("输入和输出单价必须同时配置或同时留空")
        if (
            input_per_million is not None
            and (
                input_per_million < 0
                or output_per_million is None
                or output_per_million < 0
            )
        ):
            raise ValueError("模型单价不能小于 0")
        self.input_per_million = input_per_million
        self.output_per_million = output_per_million

    @property
    def configured(self) -> bool:
        return self.input_per_million is not None

    def price(self, usage: UsageInfo) -> UsageInfo:
        estimated_cost: float | None = None
        if (
            self.input_per_million is not None
            and self.output_per_million is not None
        ):
            estimated_cost = (
                usage.input_tokens * self.input_per_million
                + usage.output_tokens * self.output_per_million
            ) / 1_000_000
        return UsageInfo(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_usd=estimated_cost,
        )


class UsageTracker:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def record(
        self,
        *,
        client_identifier: str,
        provider: str,
        usage: UsageInfo,
    ) -> UsageDailyRecord:
        return self.store.record_usage(
            client_hash=hash_client_identifier(client_identifier),
            provider=provider,
            usage=usage,
        )


class ProviderUsageControls:
    def __init__(
        self,
        *,
        enabled: bool,
        provider: str,
        rate_limiter: DailyRateLimiter,
        circuit: DailySpendCircuit,
        pricer: UsagePricer,
        tracker: UsageTracker,
    ) -> None:
        self.enabled = enabled
        self.provider = provider
        self.rate_limiter = rate_limiter
        self.circuit = circuit
        self.pricer = pricer
        self.tracker = tracker

    def before_call(self, client_identifier: str) -> None:
        if not self.enabled:
            return
        self.circuit.ensure_available()
        self.rate_limiter.check_and_increment(client_identifier)

    def after_call(
        self,
        client_identifier: str,
        usage: UsageInfo,
    ) -> UsageInfo:
        if not self.enabled:
            return usage
        priced = self.pricer.price(usage)
        self.tracker.record(
            client_identifier=client_identifier,
            provider=self.provider,
            usage=priced,
        )
        return priced
