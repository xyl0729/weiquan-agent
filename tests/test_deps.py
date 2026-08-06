from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.deps import resolve_credential


@dataclass
class FakeCircuit:
    tripped: bool = False

    def is_tripped(self) -> bool:
        return self.tripped


@dataclass
class FakeRateLimit:
    is_exceeded: bool = False

    def exceeded(self, client_ip: str) -> bool:
        assert client_ip
        return self.is_exceeded


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "key_mode": "hybrid",
        "server_api_key": "server-secret",
    }
    values.update(overrides)
    return Settings(**values)


def test_user_key_bypasses_server_limits() -> None:
    credential = resolve_credential(
        user_key=" user-secret ",
        client_ip="127.0.0.1",
        settings=make_settings(server_api_key=None),
        circuit=FakeCircuit(tripped=True),
        ratelimit=FakeRateLimit(is_exceeded=True),
    )

    assert credential.key == "user-secret"
    assert credential.source == "user"
    assert credential.metered is False


def test_byok_requires_user_key() -> None:
    with pytest.raises(HTTPException) as exc_info:
        resolve_credential(
            user_key=None,
            client_ip="127.0.0.1",
            settings=make_settings(key_mode="byok"),
            circuit=FakeCircuit(),
            ratelimit=FakeRateLimit(),
        )

    assert exc_info.value.status_code == 400


def test_tripped_circuit_rejects_server_usage() -> None:
    with pytest.raises(HTTPException) as exc_info:
        resolve_credential(
            user_key=None,
            client_ip="127.0.0.1",
            settings=make_settings(),
            circuit=FakeCircuit(tripped=True),
            ratelimit=FakeRateLimit(),
        )

    assert exc_info.value.status_code == 503


def test_exceeded_rate_limit_rejects_server_usage() -> None:
    with pytest.raises(HTTPException) as exc_info:
        resolve_credential(
            user_key=None,
            client_ip="127.0.0.1",
            settings=make_settings(),
            circuit=FakeCircuit(),
            ratelimit=FakeRateLimit(is_exceeded=True),
        )

    assert exc_info.value.status_code == 429


def test_missing_server_key_is_configuration_error() -> None:
    with pytest.raises(HTTPException) as exc_info:
        resolve_credential(
            user_key=None,
            client_ip="127.0.0.1",
            settings=make_settings(server_api_key=None),
            circuit=FakeCircuit(),
            ratelimit=FakeRateLimit(),
        )

    assert exc_info.value.status_code == 500


@pytest.mark.parametrize("mode", ["server", "hybrid"])
def test_server_credential_is_metered(mode: str) -> None:
    credential = resolve_credential(
        user_key=None,
        client_ip="127.0.0.1",
        settings=make_settings(key_mode=mode),
        circuit=FakeCircuit(),
        ratelimit=FakeRateLimit(),
    )

    assert credential.source == "server"
    assert credential.metered is True
    assert credential.key == "server-secret"

