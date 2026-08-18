from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.schemas import ConsultRequest
from app.config import Settings
from app.main import create_app
from app.providers.catalog import ProviderCatalog, ProviderResolver
from app.providers.fake import FakeProvider
from tests.test_pipeline import make_pipeline


def test_public_consult_schema_rejects_provider_selection() -> None:
    with pytest.raises(ValidationError):
        ConsultRequest(
            message="房东不退押金",
            provider_id="fake",
        )


def test_public_provider_projection_only_contains_deepseek() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        deepseek_api_key="do-not-expose",
    )

    entries = ProviderCatalog.from_settings(settings).public_entries

    assert [entry.id for entry in entries] == ["deepseek"]
    assert entries[0].is_default is True
    assert entries[0].offline is False
    assert "do-not-expose" not in entries[0].model_dump_json()


def test_public_api_never_exposes_or_selects_fake(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(create_app(pipeline.settings, pipeline=pipeline))

    catalog = client.get("/api/providers")
    rejected_body = client.post(
        "/api/consult",
        json={
            "message": "房东不退押金",
            "provider_id": "fake",
        },
        headers={
            "X-Provider-ID": "fake",
            "Cookie": "provider_id=fake",
        },
    )
    accepted = client.post(
        "/api/consult",
        json={"message": "房东不退押金"},
        headers={
            "X-Provider-ID": "fake",
            "Cookie": "provider_id=fake",
        },
    )

    assert catalog.status_code == 200
    assert [item["id"] for item in catalog.json()["providers"]] == [
        "deepseek"
    ]
    assert "fake" not in catalog.text.casefold()
    assert rejected_body.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["usage"]["provider"] == "fake"


def test_production_resolver_does_not_construct_fake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None).model_copy(
        update={
            "deployment_mode": "production",
            "llm_provider": "deepseek",
            "deepseek_api_key": SecretStr("production-test-key"),
        }
    )

    def forbidden_fake() -> FakeProvider:
        raise AssertionError("production must not construct FakeProvider")

    monkeypatch.setattr(
        "app.providers.catalog.FakeProvider",
        forbidden_fake,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        )
    )
    try:
        resolver = ProviderResolver.from_settings(
            settings,
            client=client,
        )
        assert resolver.resolve().name == "deepseek"
        assert [entry.id for entry in resolver.catalog.entries] == [
            "deepseek"
        ]
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_local_explicit_fake_injection_constructs_no_deepseek(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_deepseek(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("local fake injection must stay offline")

    monkeypatch.setattr(
        "app.providers.catalog.DeepSeekProvider",
        forbidden_deepseek,
    )
    pipeline, _ = make_pipeline(tmp_path, provider=FakeProvider())

    assert pipeline.provider.name == "fake"

