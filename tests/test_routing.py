from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.models import (
    CoverageResult,
    ExtractionResult,
    UsageInfo,
)
from app.agent.routing import (
    FORMAL_TOPIC_IDS,
    UNVERIFIED_TOPIC_IDS,
    SafetySignalGate,
    ScenarioRouter,
    TopicRegistry,
)
from app.playbooks.registry import PlaybookRegistry


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def make_registry() -> TopicRegistry:
    playbooks = PlaybookRegistry.from_directory(
        PROJECT_ROOT / "app" / "playbooks"
    )
    return TopicRegistry.from_playbooks(playbooks)


def extraction(
    topic_id: str,
    *,
    confidence: float = 0.95,
    risk_flags: list[str] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        candidate_topic_id=topic_id,
        topic_label=None,
        facts={},
        unknown_slots=[],
        risk_flags=risk_flags or [],
        confidence=confidence,
        provider="fake",
        model="fake-deterministic-v1",
        usage=UsageInfo(),
    )


def test_extraction_result_accepts_legacy_scenario_alias() -> None:
    result = ExtractionResult(
        scenario_id="deposit_deduction",
        facts={},
        provider="fake",
        model="fake-deterministic-v1",
    )

    assert result.candidate_topic_id == "deposit_deduction"
    assert result.scenario_id == "deposit_deduction"
    assert "scenario_id" not in result.model_dump()


def test_stated_goal_can_receive_direct_answer_without_question_mark() -> None:
    result = ExtractionResult(
        candidate_topic_id="return_refused",
        turn_intent="stated_goal",
        facts={"requested_resolution": "replacement"},
        explicit_question=None,
        bounded_answer=(
            "可以把补发正确商品作为当前明确诉求，并要求商家在平台内"
            "确认补发时间、物流单号和错发商品如何退回。"
        ),
        facts_to_verify=["订单商品与实收商品的差异"],
        provider="fake",
        model="fake-deterministic-v1",
    )

    assert result.turn_intent == "stated_goal"
    assert result.explicit_question is None
    assert result.bounded_answer is not None


@pytest.mark.parametrize(
    "unsafe_field",
    [
        {"coverage_mode": "formal"},
        {"playbook_id": "deposit_deduction"},
        {"citations": ["made-up-ref"]},
        {"verdict": "user_wins"},
    ],
)
def test_extraction_result_rejects_provider_authority_fields(
    unsafe_field: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "candidate_topic_id": "deposit_deduction",
        "facts": {},
        "provider": "fake",
        "model": "fake-deterministic-v1",
        **unsafe_field,
    }

    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)


def test_topic_registry_has_nine_formal_and_sixteen_unverified_topics() -> None:
    registry = make_registry()

    assert set(registry.formal_topic_ids) == set(FORMAL_TOPIC_IDS)
    assert set(registry.unverified_topic_ids) == set(
        UNVERIFIED_TOPIC_IDS
    )
    assert len(registry.topic_ids) == 25
    for topic_id in FORMAL_TOPIC_IDS:
        topic = registry.get(topic_id)
        assert topic.coverage_mode == "formal"
        assert topic.playbook_id == topic_id
    for topic_id in UNVERIFIED_TOPIC_IDS:
        topic = registry.get(topic_id)
        assert topic.coverage_mode == "unverified_guidance"
        assert topic.playbook_id is None


def test_unknown_topic_is_a_safe_unverified_long_tail() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("made_up_legal_topic"),
        message="这是一件目前无法归类的事情",
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "unknown"
    assert routed.coverage.playbook_id is None
    assert routed.facts == {}


@pytest.mark.parametrize(
    "message",
    [
        "老师打骂学生，学校一直不处理",
        "孩子在学校被同学欺凌，我该怎么办",
        "是同学霸凌，学校一直不管",
    ],
)
def test_unknown_topic_uses_only_explicit_unverified_text_fallback(
    message: str,
) -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("unknown", confidence=0.0),
        message=message,
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "education_minor_safety"
    assert routed.coverage.playbook_id is None


@pytest.mark.parametrize(
    "message",
    [
        "30元外卖里有虫子",
        "食品里吃出异物",
        "饭里发现虫子",
    ],
)
def test_natural_food_foreign_object_phrasing_routes_safely(
    message: str,
) -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("unknown", confidence=0.0),
        message=message,
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "logistics_travel_food"
    assert routed.coverage.playbook_id is None


def test_unknown_topic_cannot_infer_formal_playbook_from_text() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("unknown", confidence=0.0),
        message="房东扣押金",
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "unknown"
    assert routed.coverage.playbook_id is None


def test_router_derives_formal_coverage_from_registry() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("deposit_deduction"),
        message="房东扣了押金",
    )

    assert routed.coverage == CoverageResult(
        mode="formal",
        topic_id="deposit_deduction",
        topic_label="租房押金扣减",
        confidence=0.95,
        playbook_id="deposit_deduction",
        notice="已进入本地核验的正式处理流程。",
        risk_flags=[],
    )


def test_high_confidence_semantic_formal_candidate_survives_zero_keywords(
) -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)
    message = "出租人把我交的履约担保款全部留下了"

    assert router.registry.get("deposit_deduction").match_score(message) == 0

    routed = router.route(
        extraction("deposit_deduction", confidence=0.93),
        message=message,
    )

    assert routed.coverage.mode == "formal"
    assert routed.coverage.topic_id == "deposit_deduction"
    assert routed.coverage.playbook_id == "deposit_deduction"


def test_contextual_formal_topic_requires_matching_provider_confirmation() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("deposit_deduction"),
        message="2000元",
        contextual_formal_topic_id="deposit_deduction",
    )

    assert routed.coverage.mode == "formal"
    assert routed.coverage.topic_id == "deposit_deduction"
    assert routed.coverage.playbook_id == "deposit_deduction"


@pytest.mark.parametrize(
    "candidate",
    [
        extraction("training_refund"),
        extraction("deposit_deduction", confidence=0.2),
        extraction("unknown"),
    ],
)
def test_contextual_formal_topic_rejects_mismatch_or_low_confidence(
    candidate: ExtractionResult,
) -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        candidate,
        message="2000元",
        contextual_formal_topic_id="deposit_deduction",
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "unknown"
    assert routed.coverage.playbook_id is None


def test_current_explicit_topic_overrides_conflicting_contextual_hint() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("deposit_deduction"),
        message="培训机构不退学费",
        contextual_formal_topic_id="deposit_deduction",
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "unknown"
    assert routed.coverage.playbook_id is None


@pytest.mark.parametrize(
    "message",
    [
        "刚买的手机拆封后发现屏幕有划痕，商家不让我退",
        "网购的鞋有明显瑕疵，卖家不给退款",
        "收到的商品已经破损，平台说不能退",
    ],
)
def test_return_refused_accepts_natural_goods_problem_phrasing(
    message: str,
) -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("return_refused"),
        message=message,
    )

    assert routed.coverage.mode == "formal"
    assert routed.coverage.topic_id == "return_refused"
    assert routed.coverage.playbook_id == "return_refused"


def test_low_confidence_formal_candidate_cannot_create_formal_coverage() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("deposit_deduction", confidence=0.2),
        message="说不清楚的押金问题",
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "unknown"
    assert routed.coverage.playbook_id is None


@pytest.mark.parametrize(
    ("message", "expected_flag"),
    [
        ("他现在拿刀威胁我，我还在现场", "immediate_danger"),
        ("孩子正在学校被老师殴打", "minor_harm"),
        ("人已经昏迷并且大量出血", "urgent_medical"),
        ("我遭到性侵，对方还在威胁我", "suspected_crime"),
        ("我刚被冒充客服骗着转了钱", "fraud_loss"),
        ("监控今晚就会覆盖，证据马上消失", "evidence_loss"),
    ],
)
def test_safety_signal_gate_detects_priority_risks(
    message: str,
    expected_flag: str,
) -> None:
    flags = SafetySignalGate().detect(message)

    assert expected_flag in flags


def test_emergency_signal_overrides_formal_candidate() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("general_rental"),
        message="房东现在拿刀威胁我离开房子",
    )

    assert routed.coverage.mode == "emergency_guidance"
    assert routed.coverage.playbook_id is None
    assert "immediate_danger" in routed.coverage.risk_flags


def test_unverified_route_filters_non_generic_provider_facts() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)
    candidate = extraction("medical_service_dispute").model_copy(
        update={
            "facts": {
                "event_time": "今天",
                "request": "取得病历",
                "legal_liability": "医院承担全部责任",
            }
        }
    )

    routed = router.route(candidate, message="医院拒绝给我病历")

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.facts == {
        "event_time": "今天",
        "request": "取得病历",
    }


def test_unverified_route_filters_only_explicit_unknown_placeholders() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)
    candidate = extraction("game_account_dispute").model_copy(
        update={
            "facts": {
                "event_time": " UNKNOWN. ",
                "location": "未提供",
                "harm": "不清楚",
                "request": "要求平台说明封禁原因",
                "evidence_status": [
                    "unknown",
                    "聊天记录",
                    " 暂时没有其他材料 ",
                ],
            }
        }
    )

    routed = router.route(candidate, message="游戏账号被别人使用后封禁")

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.facts == {
        "request": "要求平台说明封禁原因",
        "evidence_status": ["聊天记录", "暂时没有其他材料"],
    }


def test_unverified_route_drops_a_list_containing_only_unknown_markers() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)
    candidate = extraction("game_account_dispute").model_copy(
        update={
            "facts": {
                "evidence_status": ["UNSPECIFIED", "无法确认", " n/a "],
            }
        }
    )

    routed = router.route(candidate, message="游戏账号被别人使用后封禁")

    assert routed.facts == {}


def test_followup_without_new_topic_signal_keeps_previous_topic() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("payment_fraud"),
        message="我联系不上人，他把我删除了，我该怎么办",
        previous_topic_id="game_account_dispute",
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "game_account_dispute"


def test_explicit_new_topic_signal_can_switch_from_previous_topic() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("general_rental"),
        message="另外一件事，房东一直不维修，我想提前退租",
        previous_topic_id="game_account_dispute",
    )

    assert routed.coverage.mode == "unverified_guidance"
    assert routed.coverage.topic_id == "general_rental"


def test_explicit_fraud_signal_still_routes_to_payment_fraud() -> None:
    router = ScenarioRouter(make_registry(), min_confidence=0.45)

    routed = router.route(
        extraction("payment_fraud"),
        message="另外有人冒充客服骗我转账",
        previous_topic_id="game_account_dispute",
    )

    assert routed.coverage.mode == "emergency_guidance"
    assert routed.coverage.topic_id == "payment_fraud"
    assert "fraud_loss" in routed.coverage.risk_flags
