from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.agent.progression import (
    VisibleTurnContent,
    classify_turn_intent,
    comparison_is_equivalent,
    comparison_units,
    derive_unverified_stage,
    find_duplicate,
    has_unverified_stage_signal,
    is_continuation_message,
    is_direct_question,
    is_more_specific_question,
    more_precise_question,
    normalize_visible_text,
    project_response,
    requested_unverified_stage,
    requires_direct_answer,
)
from app.db.models import TurnRecord


def _turn(response: dict, *, message: str = "继续") -> TurnRecord:
    return TurnRecord(
        id=str(uuid4()),
        owner_id=str(uuid4()),
        session_id=str(uuid4()),
        user_message=message,
        response=response,
        created_at=datetime.now(UTC),
    )


def test_normalization_ignores_width_case_punctuation_and_numbering() -> None:
    assert normalize_visible_text("  1．ＡＢＣ，下一步！ ") == "abc下一步"
    assert normalize_visible_text("一、 保存 证据。") == "保存证据"
    assert normalize_visible_text("（继续）") == "继续"


def test_continuation_intent_is_strict_and_deterministic() -> None:
    assert is_continuation_message("继续")
    assert is_continuation_message(" 然后呢？ ")
    assert is_continuation_message("下一步怎么办")
    assert not is_continuation_message("继续联系商家，他拒绝了")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("如果商家一直不理我怎么办？", "question"),
        ("我希望商家重新发货", "stated_goal"),
        (
            "我想让商家补发正确商品，应该怎么处理？",
            "stated_goal",
        ),
        ("我的诉求是补发正确商品", "stated_goal"),
        ("我已经联系商家但被拒绝了", "completed_action"),
        ("我已经拍照，但吃掉了一部分", "completed_action"),
        ("继续", "continue_case"),
        ("不是退款，我要的是补发", "correction"),
        ("另外一件事，我的工资也没发", "new_case"),
        ("订单上写的是蓝色，收到的是黑色", "new_fact"),
    ],
)
def test_turn_intent_distinguishes_goals_actions_and_questions(
    message: str,
    expected: str,
) -> None:
    assert classify_turn_intent(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "继续",
        "我希望商家重新发货",
        "我的诉求是让对方补发",
        "订单上写的是蓝色，收到的是黑色",
    ],
)
def test_unverified_stage_does_not_advance_without_completed_action(
    message: str,
) -> None:
    intent = classify_turn_intent(message)

    assert requested_unverified_stage(message, 3, turn_intent=intent) == 3
    assert has_unverified_stage_signal(message) is False


@pytest.mark.parametrize(
    ("message", "minimum_stage"),
    [
        ("我已经联系商家但被拒绝了", 6),
        ("书面发过去三天了，商家一直没回复", 5),
        ("我已经向平台投诉并拿到受理编号", 7),
    ],
)
def test_unverified_stage_advances_only_from_explicit_status(
    message: str,
    minimum_stage: int,
) -> None:
    intent = classify_turn_intent(message)

    assert intent == "completed_action"
    assert requested_unverified_stage(
        message,
        3,
        turn_intent=intent,
    ) >= minimum_stage
    assert has_unverified_stage_signal(message)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("我该怎么办", True),
        ("这个需要怎么处理", True),
        ("没有对方身份信息能否起诉", True),
        ("起诉需要准备哪些材料", True),
        ("继续", False),
        ("下一步", False),
        ("下一步怎么办", False),
    ],
)
def test_direct_answer_requirement_distinguishes_real_questions_from_progress(
    message: str,
    expected: bool,
) -> None:
    if expected:
        assert is_direct_question(message)
    assert requires_direct_answer(message) is expected


def test_projects_current_and_legacy_response_shapes_safely() -> None:
    current = project_response(
        {
            "turn_kind": "plan_update",
            "reply": {
                "text": "金额变更不影响结论。",
                "suggested_actions": ["保存新的付款记录"],
            },
            "plan": {
                "summary": "当前结论",
                "actions": ["发送书面通知"],
                "evidence_now": ["付款记录"],
                "communication_text": "请在三日内回复。",
                "limitations": ["仍需核对合同。"],
            },
        }
    )
    legacy = project_response(
        {
            "questions": ["请补充合同日期？"],
            "limitations": [],
        }
    )
    malformed = project_response({"plan": "not-an-object", "reply": []})

    assert current.turn_kind == "plan_update"
    assert normalize_visible_text("金额变更不影响结论。") in current.replies
    assert normalize_visible_text("发送书面通知") in current.actions
    assert legacy.turn_kind == "fact_collection"
    assert legacy.questions == (normalize_visible_text("请补充合同日期？"),)
    assert malformed.core_units == ()


def test_projects_guidance_direct_answer_as_conversation_reply() -> None:
    projected = project_response(
        {
            "turn_kind": "unverified_guidance",
            "guidance": {
                "direct_answer": (
                    "现有信息不足以判断能否直接起诉，需要先核对对方身份。"
                ),
                "actions": ["先保存借号约定"],
            },
        }
    )

    assert normalize_visible_text(
        "现有信息不足以判断能否直接起诉，需要先核对对方身份。"
    ) in projected.replies


def test_exact_near_text_and_structural_duplicates_are_rejected() -> None:
    previous = VisibleTurnContent(
        replies=(
            normalize_visible_text(
                "先保存对方拒绝处理的记录，再向平台提交书面投诉。"
            ),
        ),
        actions=(normalize_visible_text("向平台提交书面投诉"),),
    )
    exact = VisibleTurnContent(
        replies=(
            normalize_visible_text(
                "先保存对方拒绝处理的记录，再向平台提交书面投诉！"
            ),
        ),
        actions=(normalize_visible_text("向平台提交书面投诉"),),
    )
    near = VisibleTurnContent(
        replies=(
            normalize_visible_text(
                "请先保存对方拒绝处理的记录，然后向平台提交书面投诉。"
            ),
        ),
        actions=(normalize_visible_text("向平台提交书面投诉"),),
    )
    structural = VisibleTurnContent(
        actions=previous.actions,
        evidence=(normalize_visible_text("订单截图"),),
    )
    structural_history = VisibleTurnContent(
        actions=previous.actions,
        evidence=(normalize_visible_text("订单截图"),),
    )

    assert find_duplicate(exact, (previous,)).reason == "exact"
    assert find_duplicate(near, (previous,)).reason == "near_text"
    assert (
        find_duplicate(structural, (structural_history,)).reason
        == "exact"
    )


def test_new_action_or_fact_impact_is_not_a_false_positive() -> None:
    previous = VisibleTurnContent(
        replies=(normalize_visible_text("先保存订单记录。"),),
        actions=(normalize_visible_text("保存订单记录"),),
    )
    candidate = VisibleTurnContent(
        replies=(
            normalize_visible_text(
                "你补充的付款日期不改变当前结论；下一步设置书面回复期限。"
            ),
        ),
        actions=(normalize_visible_text("设置书面回复期限"),),
    )

    result = find_duplicate(candidate, (previous,))

    assert result.duplicate is False
    assert result.novel_units


def test_new_fact_with_repeated_question_is_not_rejected() -> None:
    previous = VisibleTurnContent(
        replies=(normalize_visible_text("先等待平台答复。"),),
        questions=(normalize_visible_text("平台是否已经回复？"),),
    )
    candidate = VisibleTurnContent(
        replies=(
            normalize_visible_text(
                "你补充的拒绝截图可以证明平台已经明确拒绝处理。"
            ),
        ),
        questions=(normalize_visible_text("平台是否已经回复？"),),
    )

    result = find_duplicate(candidate, (previous,))

    assert result.duplicate is False
    assert normalize_visible_text(
        "你补充的拒绝截图可以证明平台已经明确拒绝处理。"
    ) in result.novel_units


def test_field_identity_keeps_new_action_distinct_from_old_reply() -> None:
    text = "向平台提交书面投诉并保留受理编号"
    previous = VisibleTurnContent(
        replies=(normalize_visible_text(text),),
    )
    candidate = VisibleTurnContent(
        actions=(normalize_visible_text(text),),
    )

    assert find_duplicate(candidate, (previous,)).duplicate is False


def test_new_stage_is_not_blocked_by_old_guidance_text() -> None:
    previous = VisibleTurnContent(
        actions=(normalize_visible_text("整理订单和付款记录"),),
        communications=(normalize_visible_text("首次书面联系商家"),),
    )
    candidate = VisibleTurnContent(
        replies=(
            normalize_visible_text(
                "对方已经拒绝处理，现在进入书面催办阶段。"
            ),
        ),
        actions=(normalize_visible_text("设置明确回复日期并书面催办"),),
        communications=(normalize_visible_text("首次书面联系商家"),),
    )

    assert find_duplicate(candidate, (previous,)).duplicate is False


def test_repeated_question_is_rejected_but_precise_question_is_allowed() -> None:
    broad = "合同是什么时候签的？"
    precise = more_precise_question(broad, slot_type="date")
    previous = VisibleTurnContent(
        questions=(normalize_visible_text(broad),),
    )
    repeated = VisibleTurnContent(
        questions=(normalize_visible_text("合同是什么时候签的。"),),
    )
    refined = VisibleTurnContent(
        questions=(normalize_visible_text(precise),),
    )

    assert find_duplicate(repeated, (previous,)).reason == "repeated_question"
    assert is_more_specific_question(precise, broad)
    assert find_duplicate(refined, (previous,)).duplicate is False


def test_different_direct_question_is_allowed() -> None:
    previous = VisibleTurnContent(
        questions=(normalize_visible_text("合同是什么时候签的？"),),
    )
    candidate = VisibleTurnContent(
        questions=(normalize_visible_text("合同约定的退款期限是多久？"),),
    )

    assert find_duplicate(candidate, (previous,)).duplicate is False


def test_static_notice_alone_never_counts_as_progress() -> None:
    candidate = VisibleTurnContent(
        notices=(normalize_visible_text("本结果不构成法律意见。"),),
    )

    result = find_duplicate(candidate, ())

    assert result.duplicate is True
    assert result.reason == "same_structure"


def test_first_fact_collection_exhaustion_is_a_stage_transition() -> None:
    asking = project_response(
        {
            "turn_kind": "fact_collection",
            "followup_round": 2,
            "can_ask_more": False,
            "questions": ["合同中是否约定可以扣除押金？"],
            "limitations": [],
        }
    )
    exhausted = project_response(
        {
            "turn_kind": "fact_collection",
            "followup_round": 2,
            "can_ask_more": False,
            "questions": [],
            "limitations": [
                "已达到两轮追问上限，现有事实不足以形成确定性判断。"
            ],
        }
    )

    first = find_duplicate(exhausted, (asking,))
    repeated = find_duplicate(exhausted, (asking, exhausted))

    assert exhausted.stages == ("factcollectionexhausted",)
    assert first.duplicate is False
    assert repeated.duplicate is True


def test_transaction_comparison_units_preserve_field_identity() -> None:
    text = "向平台提交书面投诉并保留受理编号"
    candidate = VisibleTurnContent(
        actions=(normalize_visible_text(text),),
    )
    previous_reply = {
        "turn_kind": "followup_answer",
        "reply": {"text": text},
    }
    previous_action = {
        "turn_kind": "followup_answer",
        "reply": {"suggested_actions": [text]},
    }

    encoded = comparison_units(candidate)

    assert encoded == (f"action:{normalize_visible_text(text)}",)
    assert comparison_is_equivalent(encoded, previous_reply) is False
    assert comparison_is_equivalent(encoded, previous_action) is True


def test_legacy_untyped_transaction_units_remain_readable() -> None:
    text = normalize_visible_text(
        "先保存对方拒绝处理的记录，再向平台提交书面投诉。"
    )
    response = {
        "turn_kind": "followup_answer",
        "reply": {
            "text": "先保存对方拒绝处理的记录，再向平台提交书面投诉。"
        },
    }

    assert comparison_is_equivalent((text,), response) is True


def test_projection_tolerates_legacy_and_invalid_nested_fields() -> None:
    legacy_reply = project_response(
        {
            "turn_kind": "followup_answer",
            "reply": {
                "text": "先保存沟通记录。",
                "suggested_actions": "向平台投诉",
            },
            "guidance": {
                "actions": {"unexpected": "shape"},
                "communication_guide": {
                    "message": ["请在三日内回复。", 123],
                },
            },
            "coverage": {"risk_flags": "minor_harm"},
        }
    )
    missing = project_response(
        {
            "turn_kind": "plan_update",
            "plan": {
                "actions": [None, "书面催办"],
                "communication_guide": None,
            },
        }
    )

    assert legacy_reply.turn_kind == "followup_answer"
    assert normalize_visible_text("向平台投诉") in legacy_reply.actions
    assert legacy_reply.risk_flags == ()
    assert missing.turn_kind == "plan_update"
    assert missing.actions == (normalize_visible_text("书面催办"),)


def test_unverified_stage_recovers_from_coverage_and_short_replies() -> None:
    turns = [
        _turn(
            {
                "turn_kind": "unverified_guidance",
                "coverage": {
                    "mode": "unverified_guidance",
                    "topic_id": "medical_service_dispute",
                },
                "guidance": {
                    "actions": ["先确认当前安全"],
                    "evidence_now": ["保存病历"],
                    "communication_guide": {},
                    "limitations": [],
                    "next_question": "是否已经取得病历？",
                },
            }
        ),
        _turn(
            {
                "turn_kind": "followup_answer",
                "reply": {
                    "text": "现在进行首次书面联系，并保存送达记录。",
                    "suggested_actions": ["通过可留痕渠道发送"],
                },
            }
        ),
    ]

    assert (
        derive_unverified_stage(
            turns,
            topic_id="medical_service_dispute",
        )
        == 3
    )
