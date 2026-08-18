from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine, RowMapping


ProviderOutcome = Literal[
    "success",
    "timeout",
    "network_error",
    "rate_limited",
    "server_error",
    "invalid_output",
    "rejected",
    "configuration_error",
    "provider_error",
]
ProviderHealthStatus = Literal["unknown", "healthy", "degraded"]


class ProviderCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    logical_call_id: UUID = Field(default_factory=uuid4)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    outcome: ProviderOutcome
    duration_ms: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    occurred_at: datetime


class ProviderHealthState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=50)
    status: ProviderHealthStatus
    sample_count: int = Field(ge=0, le=10)
    failure_count: int = Field(ge=0, le=10)
    consecutive_successes: int = Field(ge=0, le=10)
    consecutive_failures: int = Field(ge=0, le=10)
    last_result_at: datetime | None = None
    updated_at: datetime


class ProviderHealthStore(Protocol):
    def add_result(self, result: ProviderCallResult) -> None: ...

    def recent_results(
        self,
        provider: str,
        *,
        since: datetime,
        limit: int,
    ) -> Sequence[ProviderCallResult]: ...

    def get_state(self, provider: str) -> ProviderHealthState | None: ...

    def save_state(self, state: ProviderHealthState) -> None: ...


class InMemoryProviderHealthStore:
    def __init__(self) -> None:
        self._results: list[ProviderCallResult] = []
        self._states: dict[str, ProviderHealthState] = {}
        self._lock = threading.RLock()

    def add_result(self, result: ProviderCallResult) -> None:
        with self._lock:
            if any(item.id == result.id for item in self._results):
                return
            self._results.append(result)

    def recent_results(
        self,
        provider: str,
        *,
        since: datetime,
        limit: int,
    ) -> Sequence[ProviderCallResult]:
        with self._lock:
            matches = [
                result
                for result in self._results
                if result.provider == provider
                and result.occurred_at >= since
            ]
        matches.sort(
            key=lambda result: (result.occurred_at, str(result.id)),
            reverse=True,
        )
        return tuple(matches[:limit])

    def get_state(self, provider: str) -> ProviderHealthState | None:
        with self._lock:
            return self._states.get(provider)

    def save_state(self, state: ProviderHealthState) -> None:
        with self._lock:
            self._states[state.provider] = state


class PostgresProviderHealthStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def add_result(self, result: ProviderCallResult) -> None:
        from app.db.tables import provider_call_results

        values = result.model_dump(mode="python")
        values["id"] = str(values["id"])
        values["logical_call_id"] = str(values["logical_call_id"])
        with self.engine.begin() as connection:
            connection.execute(
                pg_insert(provider_call_results)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[provider_call_results.c.id]
                )
            )

    def recent_results(
        self,
        provider: str,
        *,
        since: datetime,
        limit: int,
    ) -> Sequence[ProviderCallResult]:
        from app.db.tables import provider_call_results

        with self.engine.connect() as connection:
            rows = connection.execute(
                select(provider_call_results)
                .where(
                    provider_call_results.c.provider == provider,
                    provider_call_results.c.occurred_at >= since,
                )
                .order_by(
                    provider_call_results.c.occurred_at.desc(),
                    provider_call_results.c.id.desc(),
                )
                .limit(limit)
            ).mappings().all()
        return tuple(_result_from_row(row) for row in rows)

    def get_state(self, provider: str) -> ProviderHealthState | None:
        from app.db.tables import provider_health_states

        with self.engine.connect() as connection:
            row = connection.execute(
                select(provider_health_states).where(
                    provider_health_states.c.provider == provider
                )
            ).mappings().one_or_none()
        return _state_from_row(row) if row is not None else None

    def save_state(self, state: ProviderHealthState) -> None:
        from app.db.tables import provider_health_states

        values = state.model_dump(mode="python")
        with self.engine.begin() as connection:
            connection.execute(
                pg_insert(provider_health_states)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[provider_health_states.c.provider],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key != "provider"
                    },
                )
            )


def _result_from_row(row: RowMapping) -> ProviderCallResult:
    return ProviderCallResult.model_validate(dict(row))


def _state_from_row(row: RowMapping) -> ProviderHealthState:
    return ProviderHealthState.model_validate(dict(row))
