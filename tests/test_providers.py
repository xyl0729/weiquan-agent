from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from app.agent.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderOutputError,
)
from app.config import Settings
from app.providers.deepseek import DeepSeekProvider
from app.providers.factory import create_provider
from app.providers.fake import FakeProvider


EXTRACTION_CONTEXT: dict[str, object] = {
    "allowed_scenario_ids": ["deposit_deduction"],
    "allowed_slot_names": [
        "deposit_amount",
        "withheld_amount",
        "landlord_reason",
        "has_checkout_photos",
    ],
    "required_slot_names": [
        "deposit_amount",
        "withheld_amount",
        "landlord_reason",
    ],
    "existing_facts": {},
}

MULTI_SCENARIO_CONTEXT: dict[str, object] = {
    "allowed_scenario_ids": [
        "deposit_deduction",
        "return_refused",
    ],
    "allowed_slot_names": [
        "deposit_amount",
        "withheld_amount",
        "landlord_reason",
        "issue_type",
        "purchase_amount",
    ],
    "required_slot_names": [
        "deposit_amount",
        "withheld_amount",
        "landlord_reason",
        "issue_type",
    ],
    "scenario_definitions": {
        "deposit_deduction": {
            "allowed_slot_names": [
                "deposit_amount",
                "withheld_amount",
                "landlord_reason",
            ],
            "required_slot_names": [
                "deposit_amount",
                "withheld_amount",
                "landlord_reason",
            ],
        },
        "return_refused": {
            "allowed_slot_names": [
                "issue_type",
                "purchase_amount",
            ],
            "required_slot_names": ["issue_type"],
        },
    },
    "existing_facts": {},
}


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def deepseek_response(
    content: object,
    *,
    status_code: int = 200,
    request_id: str = "chatcmpl-test",
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "id": request_id,
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
            },
        },
    )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_fake_provider_is_deterministic_and_filters_slots() -> None:
    provider = FakeProvider()

    result = run(
        provider.extract_facts(
            "房东因正常损耗扣了2000元押金，我有退房照片",
            EXTRACTION_CONTEXT,
        )
    )

    assert result.scenario_id == "deposit_deduction"
    assert result.facts == {
        "deposit_amount": 2000.0,
        "withheld_amount": 2000.0,
        "landlord_reason": "normal_wear",
        "has_checkout_photos": True,
    }
    assert result.unknown_slots == []
    assert result.provider == "fake"
    assert result.usage.total_tokens == 0


def test_fake_provider_reports_only_unfilled_required_slots() -> None:
    context = {
        **EXTRACTION_CONTEXT,
        "existing_facts": {"deposit_amount": 1500},
    }

    result = run(FakeProvider().extract_facts("房东不退押金", context))

    assert result.unknown_slots == [
        "withheld_amount",
        "landlord_reason",
    ]


def test_fake_provider_uses_only_selected_scenario_requirements() -> None:
    result = run(
        FakeProvider().extract_facts(
            "网购商品有质量问题，商家拒绝退货，价款800元",
            MULTI_SCENARIO_CONTEXT,
        )
    )

    assert result.scenario_id == "return_refused"
    assert result.facts == {
        "issue_type": "quality_problem",
        "purchase_amount": 800.0,
    }
    assert result.unknown_slots == []


def test_provider_factory_defaults_to_fake() -> None:
    provider = create_provider(Settings(_env_file=None))

    assert isinstance(provider, FakeProvider)


def test_provider_factory_rejects_deepseek_without_key() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        deepseek_api_key=None,
    )

    with pytest.raises(ProviderConfigurationError) as exc_info:
        create_provider(settings)

    assert exc_info.value.category == "provider_configuration"
    assert "DEEPSEEK_API_KEY" in exc_info.value.safe_message


def test_deepseek_provider_sends_scoped_json_request() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return deepseek_response(
            json.dumps(
                {
                    "scenario_id": "deposit_deduction",
                    "facts": {
                        "deposit_amount": 2000,
                        "withheld_amount": 2000,
                        "landlord_reason": "normal_wear",
                    },
                    "unknown_slots": [],
                    "confidence": 0.98,
                },
                ensure_ascii=False,
            )
        )

    async def exercise() -> object:
        async with make_client(handler) as client:
            provider = DeepSeekProvider(
                api_key="secret-value",
                base_url="https://deepseek.example/v1",
                client=client,
            )
            return await provider.extract_facts(
                "房东扣押金",
                EXTRACTION_CONTEXT,
            )

    result = run(exercise())

    assert result.scenario_id == "deposit_deduction"
    assert result.provider == "deepseek"
    assert result.model == "deepseek-chat"
    assert result.request_id == "chatcmpl-test"
    assert result.usage.total_tokens == 20
    assert captured["url"] == (
        "https://deepseek.example/v1/chat/completions"
    )
    assert captured["authorization"] == "Bearer secret-value"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert "verdict" in body["messages"][0]["content"]


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_deepseek_provider_retries_retryable_statuses(
    status_code: int,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status_code, request=request)
        return deepseek_response(
            json.dumps(
                {
                    "scenario_id": "deposit_deduction",
                    "facts": {},
                    "unknown_slots": [
                        "deposit_amount",
                        "withheld_amount",
                        "landlord_reason",
                    ],
                    "confidence": 0.5,
                }
            )
        )

    async def exercise() -> object:
        async with make_client(handler) as client:
            provider = DeepSeekProvider(
                api_key="secret",
                max_retries=1,
                client=client,
            )
            return await provider.extract_facts(
                "房东扣押金",
                EXTRACTION_CONTEXT,
            )

    result = run(exercise())

    assert result.scenario_id == "deposit_deduction"
    assert calls == 2


def test_deepseek_provider_retries_timeout_and_redacts_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(
            "secret-value should never escape",
            request=request,
        )

    async def exercise() -> None:
        async with make_client(handler) as client:
            provider = DeepSeekProvider(
                api_key="secret-value",
                max_retries=1,
                client=client,
            )
            await provider.extract_facts(
                "房东扣押金",
                EXTRACTION_CONTEXT,
            )

    with pytest.raises(ProviderError) as exc_info:
        run(exercise())

    assert calls == 2
    assert exc_info.value.category == "provider_timeout"
    assert "secret-value" not in str(exc_info.value)


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        "[]",
        json.dumps(
            {
                "scenario_id": "unknown_scenario",
                "facts": {},
                "unknown_slots": [],
            }
        ),
        json.dumps(
            {
                "scenario_id": "deposit_deduction",
                "facts": {"not_an_allowed_slot": "x"},
                "unknown_slots": [],
            }
        ),
        json.dumps(
            {
                "scenario_id": "deposit_deduction",
                "facts": {},
                "unknown_slots": [],
                "verdict": "tenant_wins",
            }
        ),
        json.dumps(
            {
                "scenario_id": "deposit_deduction",
                "facts": {
                    "landlord_reason": {
                        "authorization": "Bearer secret"
                    }
                },
                "unknown_slots": [],
            }
        ),
    ],
)
def test_deepseek_provider_rejects_invalid_or_unsafe_output(
    content: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return deepseek_response(content)

    async def exercise() -> None:
        async with make_client(handler) as client:
            provider = DeepSeekProvider(
                api_key="secret",
                client=client,
            )
            await provider.extract_facts(
                "房东扣押金",
                EXTRACTION_CONTEXT,
            )

    with pytest.raises(ProviderOutputError):
        run(exercise())


def test_deepseek_provider_rejects_empty_direct_key() -> None:
    provider = DeepSeekProvider(api_key=" ")

    with pytest.raises(ProviderConfigurationError):
        run(provider.extract_facts("房东扣押金", EXTRACTION_CONTEXT))


@pytest.mark.parametrize(
    ("facts", "unknown_slots"),
    [
        ({"deposit_amount": 800}, []),
        ({"issue_type": "quality_problem"}, ["deposit_amount"]),
    ],
)
def test_deepseek_provider_rejects_cross_scenario_slots(
    facts: dict[str, object],
    unknown_slots: list[str],
) -> None:
    content = json.dumps(
        {
            "scenario_id": "return_refused",
            "facts": facts,
            "unknown_slots": unknown_slots,
            "confidence": 0.95,
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return deepseek_response(content)

    async def exercise() -> None:
        async with make_client(handler) as client:
            provider = DeepSeekProvider(
                api_key="secret",
                client=client,
            )
            await provider.extract_facts(
                "商家拒绝退货",
                MULTI_SCENARIO_CONTEXT,
            )

    with pytest.raises(ProviderOutputError):
        run(exercise())
