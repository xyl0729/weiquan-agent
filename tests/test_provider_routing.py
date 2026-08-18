from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.agent.errors import InvalidProviderError, ProviderUnavailableError
from app.agent.routing import TopicRegistry, UNVERIFIED_TOPIC_IDS
from app.config import Settings
from app.playbooks.registry import PlaybookRegistry
from app.providers.catalog import ProviderCatalog, ProviderResolver
from app.providers.deepseek import DeepSeekProvider
from app.providers.fake import FakeProvider


PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOPIC_SAMPLES = {
    "education_minor_safety": "老师打骂学生，学校一直不处理",
    "medical_service_dispute": "医院不肯给我病历，医疗收费也说不清",
    "traffic_accident": "发生交通事故，对方撞车后离开了",
    "personal_injury": "我在商场摔伤了，想保留现场证据",
    "labor_termination": "公司突然辞退我，也没有书面解除通知",
    "wage_social_insurance": "单位拖欠工资，还一直没有缴社保",
    "workplace_harassment": "领导持续职场骚扰并打压我",
    "debt_collection": "朋友借钱不还，我有借条和转账记录",
    "payment_fraud": "有人冒充客服骗我转账",
    "general_rental": "房东一直不维修，我想提前退租",
    "property_neighbor": "楼上漏水，物业多次不处理",
    "privacy_reputation": "有人公开我的个人信息并造谣",
    "family_support_property": "对方不付抚养费，也不让我探望孩子",
    "service_contract": "平台会员服务没有履行，我要求退款",
    "logistics_travel_food": "快递损坏，客服拒绝处理",
    "game_account_dispute": "游戏账号借给网友后被开挂封禁了",
}


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def topic_context() -> dict[str, object]:
    playbooks = PlaybookRegistry.from_directory(
        PROJECT_ROOT / "app" / "playbooks"
    )
    topics = TopicRegistry.from_playbooks(playbooks)
    return {
        **playbooks.provider_context(),
        **topics.provider_context(),
    }


def test_provider_catalog_exposes_only_non_sensitive_status() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        deepseek_api_key="do-not-expose",
    )

    payload = [
        item.model_dump(mode="json")
        for item in ProviderCatalog.from_settings(settings).entries
    ]

    assert [item["id"] for item in payload] == ["fake", "deepseek"]
    assert payload[0] == {
        "id": "fake",
        "display_name": "离线演示",
        "model": "fake-deterministic-v1",
        "available": True,
        "unavailable_reason": None,
        "offline": True,
        "is_default": True,
    }
    assert payload[1]["available"] is True
    assert payload[1]["offline"] is False
    assert "do-not-expose" not in json.dumps(payload, ensure_ascii=False)


def test_provider_resolver_rejects_unknown_and_unavailable_provider() -> None:
    resolver = ProviderResolver.from_settings(
        Settings(
            _env_file=None,
            llm_provider="fake",
            deepseek_api_key=None,
        )
    )

    assert resolver.resolve().name == "fake"
    with pytest.raises(InvalidProviderError) as invalid:
        resolver.resolve("arbitrary-model")
    assert invalid.value.code == "invalid_provider"
    with pytest.raises(ProviderUnavailableError) as unavailable:
        resolver.resolve("deepseek")
    assert unavailable.value.code == "provider_unavailable"


@pytest.mark.parametrize(
    ("topic_id", "message"),
    TOPIC_SAMPLES.items(),
)
def test_fake_provider_recognizes_every_unverified_topic(
    topic_id: str,
    message: str,
) -> None:
    result = run(FakeProvider().extract_facts(message, topic_context()))

    assert result.candidate_topic_id == topic_id
    assert result.candidate_topic_id in UNVERIFIED_TOPIC_IDS
    assert result.confidence == 0.99


def test_fake_provider_returns_unknown_and_generic_candidate_facts() -> None:
    result = run(
        FakeProvider().extract_facts(
            "这件很少见的事情发生在2026年8月8日，涉及300元",
            topic_context(),
        )
    )

    assert result.candidate_topic_id == "unknown"
    assert result.facts == {
        "event_time": "2026-08-08",
        "amount": 300.0,
    }


@pytest.mark.parametrize(
    "authority_field",
    [
        {"scenario_id": "deposit_deduction"},
        {"coverage_mode": "formal"},
        {"playbook_id": "deposit_deduction"},
        {"citations": ["invented"]},
        {"verdict": "tenant_wins"},
    ],
)
def test_deepseek_candidate_contract_rejects_authority_fields(
    authority_field: dict[str, object],
) -> None:
    content = {
        "candidate_topic_id": "deposit_deduction",
        "topic_label": "租房押金扣减",
        "facts": {},
        "unknown_slots": [
            "deposit_amount",
            "withheld_amount",
            "landlord_reason",
        ],
        "risk_flags": [],
        "confidence": 0.9,
        **authority_field,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "id": "candidate-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                content,
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {},
            },
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekProvider(
                api_key="secret",
                client=client,
            )
            await provider.extract_facts(
                "房东不退押金",
                topic_context(),
            )

    from app.agent.errors import ProviderOutputError

    with pytest.raises(ProviderOutputError):
        run(exercise())
