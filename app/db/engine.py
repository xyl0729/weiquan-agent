from __future__ import annotations

from collections.abc import Iterator, Set
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.agent.errors import StorageUnavailableError
from app.config import Settings


class MigrationVersionError(RuntimeError):
    """The database schema is newer than this application supports."""


def create_database_engine(settings: Settings) -> Engine:
    dsn = settings.database_dsn
    if dsn is None or not dsn.startswith("postgresql+psycopg://"):
        raise ValueError("DATABASE_URL 必须使用 postgresql+psycopg")
    try:
        return sa_create_engine(
            dsn,
            pool_size=5,
            max_overflow=2,
            pool_pre_ping=True,
            pool_timeout=30,
            pool_recycle=1800,
            connect_args={"options": "-c timezone=UTC"},
        )
    except (ImportError, SQLAlchemyError) as exc:
        raise StorageUnavailableError() from exc


@contextmanager
def database_transaction(
    engine: Engine | Any,
) -> Iterator[Connection]:
    try:
        with engine.begin() as connection:
            yield connection
    except SQLAlchemyError as exc:
        raise StorageUnavailableError() from exc


def assert_database_revision_supported(
    revision: str,
    supported_revisions: Set[str] | set[str],
) -> None:
    if revision not in supported_revisions:
        raise MigrationVersionError(
            "数据库迁移版本高于当前应用支持范围"
        )


def probe_database(
    engine: Engine,
    *,
    supported_revisions: set[str],
) -> str:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
    except SQLAlchemyError as exc:
        raise StorageUnavailableError() from exc
    normalized = str(revision)
    assert_database_revision_supported(normalized, supported_revisions)
    return normalized
