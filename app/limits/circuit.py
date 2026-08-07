from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.agent.errors import CircuitTrippedError
from app.db.session import SessionStore


class DailySpendCircuit:
    def __init__(
        self,
        store: SessionStore,
        *,
        provider: str,
        limit_usd: float,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if limit_usd <= 0:
            raise ValueError("日费用上限必须大于 0")
        self.store = store
        self.provider = provider
        self.limit_usd = float(limit_usd)
        self._now = now or (lambda: datetime.now(UTC))

    def is_tripped(self) -> bool:
        current = _utc(self._now())
        spent = self.store.daily_estimated_cost(
            current.date(),
            provider=self.provider,
        )
        return spent >= self.limit_usd

    def ensure_available(self) -> None:
        if self.is_tripped():
            raise CircuitTrippedError()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)
