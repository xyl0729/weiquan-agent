from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from app.agent.errors import RateLimitError
from app.db.session import SessionStore


def hash_client_identifier(identifier: str) -> str:
    normalized = identifier.strip() or "unknown"
    payload = f"weiquan-agent:v1:{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class DailyRateLimiter:
    def __init__(
        self,
        store: SessionStore,
        *,
        limit: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if limit < 0:
            raise ValueError("日调用上限不能小于 0")
        self.store = store
        self.limit = limit
        self._now = now or (lambda: datetime.now(UTC))

    def check_and_increment(self, client_identifier: str) -> int:
        current = _utc(self._now())
        record = self.store.increment_rate_limit(
            client_hash=hash_client_identifier(client_identifier),
            now=current,
        )
        if record.request_count > self.limit:
            raise RateLimitError()
        return record.request_count

    def exceeded(self, client_identifier: str) -> bool:
        current = _utc(self._now())
        record = self.store.get_rate_limit(
            day=current.date(),
            client_hash=hash_client_identifier(client_identifier),
        )
        return record is not None and record.request_count >= self.limit


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)
