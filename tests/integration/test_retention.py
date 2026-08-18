from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)


def _database_url() -> str:
    value = os.getenv("TEST_POSTGRES_URL")
    if not value:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    return value


def _migrate(database_url: str) -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.replace("%", "%%"),
    )
    command.upgrade(config, "head")


def test_soft_deleted_session_is_hidden_and_outbox_survives_cascade() -> None:
    from sqlalchemy import create_engine, delete, func, select

    from app.db.postgres import PostgresApplicationStore
    from app.db.tables import (
        consultation_deletion_outbox,
        consultation_sessions,
        users,
    )

    database_url = _database_url()
    _migrate(database_url)
    engine = create_engine(database_url)
    owner_id = str(uuid4())
    other_owner_id = str(uuid4())
    store = PostgresApplicationStore(engine, now=lambda: NOW)
    try:
        with engine.begin() as connection:
            connection.execute(
                users.insert(),
                [
                    {"id": owner_id, "created_at": NOW},
                    {"id": other_owner_id, "created_at": NOW},
                ],
            )
        session = store.create_session(owner_id=owner_id, now=NOW)

        intent = store.begin_session_deletion(
            session.id,
            owner_id=owner_id,
            deleted_at=NOW,
        )

        assert intent is not None
        assert store.get_session(session.id, owner_id=owner_id) is None
        assert store.list_sessions(owner_id=owner_id) == []
        assert (
            store.begin_session_deletion(
                session.id,
                owner_id=other_owner_id,
                deleted_at=NOW,
            )
            is None
        )
        store.mark_deletion_manifest_uploaded(
            session.id,
            deleted_at=NOW,
            uploaded_at=NOW,
        )
        assert store.complete_session_deletion(
            session.id,
            deleted_at=NOW,
            completed_at=NOW,
        )
        with engine.connect() as connection:
            assert connection.execute(
                select(func.count())
                .select_from(consultation_sessions)
                .where(consultation_sessions.c.id == session.id)
            ).scalar_one() == 0
            assert connection.execute(
                select(func.count())
                .select_from(consultation_deletion_outbox)
                .where(
                    consultation_deletion_outbox.c.session_id
                    == session.id
                )
            ).scalar_one() == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(consultation_deletion_outbox).where(
                    consultation_deletion_outbox.c.session_id
                    == session.id
                )
            )
            connection.execute(
                delete(users).where(
                    users.c.id.in_([owner_id, other_owner_id])
                )
            )
        engine.dispose()


def test_retention_cleanup_is_bounded_and_cascades_content() -> None:
    from sqlalchemy import create_engine, delete, func, select

    from app.db.postgres import PostgresApplicationStore
    from app.db.tables import (
        consultation_sessions,
        consultation_turns,
        users,
    )
    from app.jobs.cleanup import PostgresCleanupStore

    database_url = _database_url()
    _migrate(database_url)
    engine = create_engine(database_url)
    owner_id = str(uuid4())
    store = PostgresApplicationStore(engine, now=lambda: NOW)
    session_ids: list[str] = []
    try:
        with engine.begin() as connection:
            connection.execute(
                users.insert().values(id=owner_id, created_at=NOW)
            )
        for offset in range(3):
            session = store.create_session(
                owner_id=owner_id,
                now=NOW - timedelta(days=31, seconds=offset),
            )
            session_ids.append(session.id)
            with engine.begin() as connection:
                connection.execute(
                    consultation_turns.insert().values(
                        id=str(uuid4()),
                        owner_id=owner_id,
                        session_id=session.id,
                        user_message="retention-sensitive-content",
                        facts={},
                        rule_matches=[],
                        response={},
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                        created_at=NOW - timedelta(days=31),
                    )
                )
        cleanup = PostgresCleanupStore(engine)

        first = cleanup.purge_expired_consultations(
            cutoff=NOW,
            limit=2,
        )
        second = cleanup.purge_expired_consultations(
            cutoff=NOW,
            limit=2,
        )
        third = cleanup.purge_expired_consultations(
            cutoff=NOW,
            limit=2,
        )

        assert (first, second, third) == (2, 1, 0)
        with engine.connect() as connection:
            assert connection.execute(
                select(func.count())
                .select_from(consultation_sessions)
                .where(consultation_sessions.c.id.in_(session_ids))
            ).scalar_one() == 0
            assert connection.execute(
                select(func.count())
                .select_from(consultation_turns)
                .where(consultation_turns.c.session_id.in_(session_ids))
            ).scalar_one() == 0
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(users).where(users.c.id == owner_id)
            )
        engine.dispose()


def test_retention_does_not_bypass_pending_deletion_manifest() -> None:
    from sqlalchemy import create_engine, delete, func, select

    from app.db.postgres import PostgresApplicationStore
    from app.db.tables import (
        consultation_deletion_outbox,
        consultation_sessions,
        users,
    )
    from app.jobs.cleanup import PostgresCleanupStore

    database_url = _database_url()
    _migrate(database_url)
    engine = create_engine(database_url)
    owner_id = str(uuid4())
    store = PostgresApplicationStore(engine, now=lambda: NOW)
    try:
        with engine.begin() as connection:
            connection.execute(
                users.insert().values(id=owner_id, created_at=NOW)
            )
        session = store.create_session(
            owner_id=owner_id,
            now=NOW - timedelta(days=31),
        )
        intent = store.begin_session_deletion(
            session.id,
            owner_id=owner_id,
            deleted_at=NOW,
        )

        assert intent is not None
        assert (
            PostgresCleanupStore(engine).purge_expired_consultations(
                cutoff=NOW,
                limit=100,
            )
            == 0
        )
        with engine.connect() as connection:
            assert connection.execute(
                select(func.count())
                .select_from(consultation_sessions)
                .where(consultation_sessions.c.id == session.id)
            ).scalar_one() == 1
            assert connection.execute(
                select(func.count())
                .select_from(consultation_deletion_outbox)
                .where(
                    consultation_deletion_outbox.c.session_id
                    == session.id
                )
            ).scalar_one() == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(consultation_deletion_outbox).where(
                    consultation_deletion_outbox.c.session_id
                    == session.id
                )
            )
            connection.execute(
                delete(users).where(users.c.id == owner_id)
            )
        engine.dispose()
