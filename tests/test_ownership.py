from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.db.contracts import LOCAL_DEVELOPMENT_OWNER_ID
from app.main import create_app
from tests.test_pipeline import make_pipeline


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", str(uuid4())),
        ("owner", str(uuid4())),
        ("owner_id", str(uuid4())),
        ("role", "admin"),
    ],
)
def test_consult_rejects_client_supplied_identity_fields(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(pipeline.settings, pipeline=pipeline)
    )

    response = client.post(
        "/api/consult",
        json={"message": "房东不退押金", field: value},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation"
    assert value not in response.text


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/consult", "post"),
        ("/api/attachments", "post"),
        ("/api/sessions", "get"),
    ],
)
def test_registered_resource_apis_require_authentication_in_test_mode(
    tmp_path: Path,
    path: str,
    method: str,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    settings = pipeline.settings.model_copy(
        update={
            "deployment_mode": "test",
            "public_base_url": "https://app.example.test",
            "cors_origins": "https://app.example.test",
        }
    )
    pipeline.settings = settings
    client = TestClient(create_app(settings, pipeline=pipeline))

    kwargs = {"json": {"message": "房东不退押金"}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "registration_required"


def test_local_mode_uses_only_the_fixed_local_principal(
    tmp_path: Path,
) -> None:
    pipeline, store = make_pipeline(tmp_path)
    client = TestClient(
        create_app(pipeline.settings, pipeline=pipeline)
    )

    response = client.post(
        "/api/consult",
        json={"message": "房东不退押金"},
    )

    assert response.status_code == 200
    session = store.require_session(response.json()["session_id"])
    assert session.owner_id == LOCAL_DEVELOPMENT_OWNER_ID

