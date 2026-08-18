from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal


MetricOutcome = Literal["success", "failure", "rejected"]
_ALLOWED_COMPONENTS = frozenset({"mail", "captcha"})
_ALLOWED_OUTCOMES = frozenset({"success", "failure", "rejected"})


@dataclass(frozen=True, slots=True)
class IntegrationMetricSnapshot:
    success: int
    failure: int
    rejected: int
    last_result_at: datetime | None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "success": self.success,
            "failure": self.failure,
            "rejected": self.rejected,
            "last_result_at": (
                self.last_result_at.isoformat()
                if self.last_result_at is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class AttachmentTempSnapshot:
    available: bool
    files: int
    bytes: int
    oldest_age_seconds: int
    truncated: bool

    def to_dict(self) -> dict[str, bool | int]:
        return {
            "available": self.available,
            "files": self.files,
            "bytes": self.bytes,
            "oldest_age_seconds": self.oldest_age_seconds,
            "truncated": self.truncated,
        }


class OperationalMetrics:
    """Keep a small, content-free window of integration outcomes."""

    def __init__(self, *, max_events_per_component: int = 1000) -> None:
        if not 10 <= max_events_per_component <= 10_000:
            raise ValueError("metric event bound must be between 10 and 10000")
        self._max_events = int(max_events_per_component)
        self._events: dict[
            str,
            deque[tuple[datetime, MetricOutcome]],
        ] = {
            component: deque()
            for component in _ALLOWED_COMPONENTS
        }
        self._lock = threading.RLock()

    def record(
        self,
        component: str,
        outcome: MetricOutcome,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        if component not in _ALLOWED_COMPONENTS:
            raise ValueError("unsupported metric component")
        if outcome not in _ALLOWED_OUTCOMES:
            raise ValueError("unsupported metric outcome")
        current = _utc(occurred_at or datetime.now(UTC))
        with self._lock:
            events = self._events[component]
            events.append((current, outcome))
            while len(events) > self._max_events:
                events.popleft()

    def snapshot(
        self,
        component: str,
        *,
        window: timedelta = timedelta(minutes=30),
        now: datetime | None = None,
    ) -> IntegrationMetricSnapshot:
        if component not in _ALLOWED_COMPONENTS:
            raise ValueError("unsupported metric component")
        if window <= timedelta(0):
            raise ValueError("metric window must be positive")
        current = _utc(now or datetime.now(UTC))
        cutoff = current - window
        with self._lock:
            events = tuple(
                (occurred_at, outcome)
                for occurred_at, outcome in self._events[component]
                if occurred_at >= cutoff
            )
        return IntegrationMetricSnapshot(
            success=sum(outcome == "success" for _, outcome in events),
            failure=sum(outcome == "failure" for _, outcome in events),
            rejected=sum(outcome == "rejected" for _, outcome in events),
            last_result_at=events[-1][0] if events else None,
        )


def attachment_temp_snapshot(
    path: Path,
    *,
    max_files: int = 10_000,
    now_epoch: float | None = None,
) -> AttachmentTempSnapshot:
    if not 1 <= max_files <= 100_000:
        raise ValueError("attachment scan bound is invalid")
    current = time.time() if now_epoch is None else float(now_epoch)
    if not path.exists():
        return AttachmentTempSnapshot(
            available=True,
            files=0,
            bytes=0,
            oldest_age_seconds=0,
            truncated=False,
        )
    if not path.is_dir():
        return AttachmentTempSnapshot(
            available=False,
            files=0,
            bytes=0,
            oldest_age_seconds=0,
            truncated=False,
        )

    files = 0
    total_bytes = 0
    oldest_age = 0
    truncated = False
    try:
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            if files >= max_files:
                truncated = True
                break
            stat = candidate.stat()
            files += 1
            total_bytes += max(0, int(stat.st_size))
            oldest_age = max(
                oldest_age,
                max(0, int(current - stat.st_mtime)),
            )
    except OSError:
        return AttachmentTempSnapshot(
            available=False,
            files=files,
            bytes=total_bytes,
            oldest_age_seconds=oldest_age,
            truncated=truncated,
        )
    return AttachmentTempSnapshot(
        available=True,
        files=files,
        bytes=total_bytes,
        oldest_age_seconds=oldest_age,
        truncated=truncated,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("metric time must include a timezone")
    return value.astimezone(UTC)
