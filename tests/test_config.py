import pytest
from pydantic import ValidationError

from app.config import Settings


def test_cors_origins_are_split_and_trimmed() -> None:
    settings = Settings(
        _env_file=None,
        cors_origins="http://localhost:8000, https://example.test ",
    )

    assert settings.allowed_origins == [
        "http://localhost:8000",
        "https://example.test",
    ]


def test_empty_server_key_is_not_a_credential() -> None:
    settings = Settings(_env_file=None, server_api_key="  ")

    assert settings.server_api_key is None


def test_unknown_key_mode_fails_early() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, key_mode="unexpected")

