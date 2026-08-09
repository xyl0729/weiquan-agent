from contextlib import contextmanager
from typing import Iterator

import pytest
from sqlalchemy.exc import OperationalError

from app.agent.errors import StorageUnavailableError
from app.config import Settings
from app.db.engine import (
    MigrationVersionError,
    assert_database_revision_supported,
    create_database_engine,
    database_transaction,
)


def postgres_settings() -> Settings:
    return Settings(
        _env_file=None,
        deployment_mode="test",
        database_url=(
            "postgresql+psycopg://weiquan:test-password@"
            "127.0.0.1:55432/weiquan_test"
        ),
    )


def test_engine_uses_bounded_pool_pre_ping_and_utc(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        "app.db.engine.sa_create_engine",
        fake_create_engine,
    )

    engine = create_database_engine(postgres_settings())

    assert engine is sentinel
    assert captured["pool_size"] == 5
    assert captured["max_overflow"] == 2
    assert captured["pool_pre_ping"] is True
    assert captured["pool_timeout"] == 30
    assert captured["connect_args"] == {"options": "-c timezone=UTC"}


def test_engine_requires_a_postgres_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        create_database_engine(Settings(_env_file=None))


def test_database_errors_map_to_safe_storage_error() -> None:
    class BrokenEngine:
        @contextmanager
        def begin(self) -> Iterator[None]:
            raise OperationalError("SELECT 1", {}, RuntimeError("secret"))
            yield

    with pytest.raises(StorageUnavailableError) as caught:
        with database_transaction(BrokenEngine()):
            pass

    assert "secret" not in str(caught.value)


def test_future_database_revision_is_rejected() -> None:
    with pytest.raises(MigrationVersionError):
        assert_database_revision_supported(
            "future-revision",
            {"20260810_0000"},
        )


def test_known_database_revision_is_accepted() -> None:
    assert_database_revision_supported(
        "20260810_0000",
        {"20260810_0000"},
    )
