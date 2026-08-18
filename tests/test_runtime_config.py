from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.schemas import RuntimeConfigResponse
from app.config import Settings
from app.main import create_app


@pytest.mark.parametrize(
    ("deployment_mode", "identity_mode"),
    [
        ("local", "local_full_test"),
        ("test", "account"),
        ("production", "account"),
    ],
)
def test_runtime_config_uses_server_deployment_mode(
    deployment_mode: str,
    identity_mode: str,
) -> None:
    settings = Settings(_env_file=None).model_copy(
        update={"deployment_mode": deployment_mode}
    )
    client = TestClient(create_app(settings))

    response = client.get("/api/runtime-config")

    assert response.status_code == 200
    assert response.json() == {"identity_mode": identity_mode}


def test_runtime_config_response_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RuntimeConfigResponse.model_validate(
            {
                "identity_mode": "local_full_test",
                "deployment_mode": "local",
            }
        )
