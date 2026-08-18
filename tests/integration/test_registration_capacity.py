from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_postgres_registration_capacity_serializes_the_101st_account() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, delete

    from app.auth.store import PostgresAuthStore
    from app.db.tables import users

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
    store = PostgresAuthStore(engine)

    try:
        with engine.begin() as connection:
            connection.execute(
                delete(users).where(users.c.email_normalized.is_not(None))
            )

        def register(index: int) -> str:
            decision = store.register_pending(
                email=f"capacity-{index}@example.test",
                password_hash="$argon2id$test-only-hash",
                policy_version="2026-08-10",
                now=NOW,
                pending_ttl=timedelta(hours=24),
            )
            return decision.status

        with ThreadPoolExecutor(max_workers=40) as executor:
            statuses = list(executor.map(register, range(101)))

        assert statuses.count("created") == 100
        assert statuses.count("capacity_full") == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(users).where(users.c.email_normalized.is_not(None))
            )
        engine.dispose()


def test_postgres_invited_capacity_counts_admin_and_serializes() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, delete, func, select

    from app.auth.store import PostgresAuthStore
    from app.db.tables import users

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.replace("%", "%%"),
    )
    command.upgrade(config, "head")
    engine = create_engine(
        database_url,
        pool_size=12,
        max_overflow=12,
        pool_pre_ping=True,
    )
    store = PostgresAuthStore(engine)

    try:
        with engine.begin() as connection:
            connection.execute(
                delete(users).where(users.c.email_normalized.is_not(None))
            )

        admin = store.create_verified_user(
            email="admin-capacity@example.com",
            password_hash="$argon2id$test-only-hash",
            role="admin",
            policy_version="2026-08-10",
            now=NOW,
            pending_ttl=timedelta(hours=24),
        )
        assert admin.status == "created"

        def invite(index: int) -> str:
            decision = store.register_pending(
                email=f"invited-capacity-{index}@example.com",
                password_hash="$argon2id$test-only-hash",
                policy_version="2026-08-10",
                now=NOW,
                pending_ttl=timedelta(hours=24),
                capacity_limit=10,
            )
            return decision.status

        with ThreadPoolExecutor(max_workers=10) as executor:
            statuses = list(executor.map(invite, range(10)))

        assert statuses.count("created") == 9
        assert statuses.count("capacity_full") == 1
        with engine.connect() as connection:
            occupied = connection.execute(
                select(func.count())
                .select_from(users)
                .where(
                    users.c.status.in_(
                        ("pending_verification", "active", "disabled")
                    )
                )
            ).scalar_one()
            admins = connection.execute(
                select(func.count())
                .select_from(users)
                .where(
                    users.c.role == "admin",
                    users.c.status == "active",
                )
            ).scalar_one()
        assert occupied == 10
        assert admins == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(users).where(users.c.email_normalized.is_not(None))
            )
        engine.dispose()
