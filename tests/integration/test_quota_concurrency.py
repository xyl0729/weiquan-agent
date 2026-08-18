from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRIAL_ID = "77777777-7777-4777-8777-777777777777"


def test_postgres_quota_serializes_concurrent_trial_reservations() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, delete

    from app.db.tables import (
        quota_buckets,
        quota_reservation_buckets,
        quota_reservations,
    )
    from app.limits.quota import QuotaExceededError
    from app.limits.reservations import PostgresQuotaStore, QuotaService

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.replace("%", "%%"),
    )
    command.upgrade(config, "head")
    engine = create_engine(
        database_url,
        pool_size=20,
        max_overflow=20,
        pool_pre_ping=True,
    )
    service = QuotaService(
        PostgresQuotaStore(engine),
        now=lambda: NOW,
    )

    try:
        with engine.begin() as connection:
            connection.execute(delete(quota_reservation_buckets))
            connection.execute(delete(quota_reservations))
            connection.execute(delete(quota_buckets))

        def reserve(index: int) -> str:
            try:
                service.reserve_trial(
                    identity_id=TRIAL_ID,
                    logical_call_id=(
                        f"80000000-0000-4000-8000-{index:012d}"
                    ),
                )
                return "reserved"
            except QuotaExceededError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=20) as executor:
            statuses = list(executor.map(reserve, range(40)))

        assert statuses.count("reserved") == 5
        assert statuses.count("rejected") == 35
        assert service.trial_status(TRIAL_ID).remaining_total == 0
    finally:
        with engine.begin() as connection:
            connection.execute(delete(quota_reservation_buckets))
            connection.execute(delete(quota_reservations))
            connection.execute(delete(quota_buckets))
        engine.dispose()
