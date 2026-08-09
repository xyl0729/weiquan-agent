import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_upgrade_head_is_repeatable_on_postgres() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.replace("%", "%%"),
    )

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            first = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            second = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
    finally:
        engine.dispose()

    assert first == "20260810_0000"
    assert second == first
