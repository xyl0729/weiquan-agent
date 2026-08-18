from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.agent.guidance import GuidanceBuilder
from app.agent.models import (
    CommunicationGuide,
    CoverageResult,
)
from app.agent.routing import UNVERIFIED_TOPIC_IDS


def coverage(
    topic_id: str,
    *,
    mode: str = "unverified_guidance",
    risk_flags: list[str] | None = None,
) -> CoverageResult:
    return CoverageResult(
        mode=mode,
        topic_id=topic_id,
        topic_label=topic_id,
        confidence=0.9,
        playbook_id=None,
        notice="当前仅提供安全与信息整理指导。",
        risk_flags=risk_flags or [],
    )


def test_communication_guide_is_strict_and_actionable() -> None:
    guide = CommunicationGuide(
        recipient="服务提供方的投诉处理负责人",
        channels=["官方客服工单", "可留痕的电子邮件"],
        when_to_send="整理现有凭证后尽快发送",
        objective="要求确认收到并说明处理安排",
        message="您好，我想书面反映本次服务争议，请确认收到并告知处理安排。",
        after_sending=["保存发送页面和回复记录"],
        escalation=["没有回应时向相应主管机构咨询"],
        required_before_send=[],
    )

    assert guide.channels[0] == "官方客服工单"
    with pytest.raises(ValidationError):
        CommunicationGuide.model_validate(
            {
                **guide.model_dump(),
                "channels": [],
            }
        )


@pytest.mark.parametrize("topic_id", UNVERIFIED_TOPIC_IDS)
def test_each_unverified_topic_builds_bounded_neutral_guidance(
    topic_id: str,
) -> None:
    result = GuidanceBuilder().build(
        coverage(topic_id),
        facts={},
    )
    serialized = result.model_dump_json()

    assert result.evidence_now
    assert result.actions
    assert result.limitations
    assert result.communication_guide.message
    assert len([result.next_question] if result.next_question else []) <= 1
    assert "法律依据" not in serialized
    assert "一定赔偿" not in serialized
    assert "必然赔偿" not in serialized
    assert "已经违法" not in serialized
    assert "应当承担全部责任" not in serialized
    assert "本项目" not in serialized
    assert "本地法条" not in serialized
    assert "确定性规则" not in serialized


def test_unknown_topic_gets_useful_guidance_without_old_category_menu() -> None:
    result = GuidanceBuilder().build(
        coverage("unknown"),
        facts={},
    )

    assert result.evidence_now
    assert result.actions
    assert result.next_question
    assert "租赁、消费、劳动" not in result.next_question


def test_game_account_guidance_prioritizes_account_security_and_appeal() -> None:
    result = GuidanceBuilder().build(
        coverage("game_account_dispute"),
        facts={"amount": 4000},
    )

    serialized = result.model_dump_json()
    assert "修改密码" in result.actions[0]
    assert "游戏平台官方入口申诉封禁" in result.actions[1]
    assert "借号约定" in serialized
    assert "充值记录" in serialized
    assert "承担全部责任" not in serialized
    assert "一定胜诉" not in serialized


@pytest.mark.parametrize(
    ("risk_flag", "first_action_fragment"),
    [
        ("immediate_danger", "离开危险"),
        ("minor_harm", "停止伤害"),
        ("urgent_medical", "就医"),
        ("suspected_crime", "人身安全"),
        ("fraud_loss", "停止转账"),
        ("evidence_loss", "安全的前提"),
    ],
)
def test_emergency_guidance_prioritizes_immediate_safety(
    risk_flag: str,
    first_action_fragment: str,
) -> None:
    result = GuidanceBuilder().build(
        coverage(
            "unknown",
            mode="emergency_guidance",
            risk_flags=[risk_flag],
        ),
        facts={},
    )

    assert first_action_fragment in result.actions[0]
    assert any(
        "不要为了取证继续置身危险" in item
        for item in result.limitations
    )
    assert re.search(
        r"(?<!\d)(?:110|119|120|123\d{2})(?!\d)",
        result.model_dump_json(),
    ) is None


def test_guidance_uses_confirmed_generic_facts_without_inventing_values() -> None:
    result = GuidanceBuilder().build(
        coverage("payment_fraud"),
        facts={
            "event_time": "今天上午",
            "amount": 2800,
            "request": "核查这笔转账",
        },
    )

    message = result.communication_guide.message
    assert "今天上午" in message
    assert "2800" in message
    assert "核查这笔转账" in message
    assert "某某" not in message
    assert "示例日期" not in message


def test_guidance_never_renders_unknown_fact_placeholders() -> None:
    result = GuidanceBuilder().build(
        coverage("game_account_dispute"),
        facts={
            "event_time": "unknown",
            "location": " 未说明 ",
            "evidence_status": ["not provided", "平台申诉记录"],
            "request": "要求平台核对封禁记录",
        },
    )

    message = result.communication_guide.message
    assert "unknown" not in message.casefold()
    assert "未说明" not in message
    assert "not provided" not in message.casefold()
    assert "平台申诉记录" in message
    assert "要求平台核对封禁记录" in message


def test_unverified_followup_stages_are_distinct_and_topic_specific() -> None:
    builder = GuidanceBuilder()
    subject = coverage("medical_service_dispute")

    stages = [
        builder.build_unverified_stage(subject, stage=stage)
        for stage in range(1, 8)
    ]

    assert [item.stage for item in stages] == list(range(1, 8))
    assert len({item.text for item in stages}) == 7
    assert len({item.action for item in stages}) == 7
    assert "医疗机构" in stages[2].action
    assert "卫生健康主管机构" in stages[5].action
    assert stages[-1].next_question is None


@pytest.mark.parametrize("stage", [0, 8])
def test_unverified_followup_rejects_out_of_range_stage(stage: int) -> None:
    with pytest.raises(ValueError, match="阶段"):
        GuidanceBuilder().build_unverified_stage(
            coverage("unknown"),
            stage=stage,
        )
