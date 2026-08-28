from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.grounding import (
    GroundedAnswerComposition,
    GroundingPacket,
    GroundingStatute,
    build_local_answer,
    general_basis_refs,
    merge_grounded_answer,
    should_compose_grounded_answer,
)
from app.retrieval.database import StatuteRecord


def general_statute(ref: str = "民法典.第五百七十七条") -> GroundingStatute:
    return GroundingStatute(
        statute_id=ref,
        law_name="中华人民共和国民法典",
        article_no="第五百七十七条",
        verified_text=(
            "第五百七十七条　当事人一方不履行合同义务或者履行合同"
            "义务不符合约定的，应当承担继续履行、采取补救措施或者"
            "赔偿损失等违约责任。"
        ),
        official_url=(
            "https://flk.npc.gov.cn/detail?id="
            "ff808081729d1efe01729d50b5c500bf"
        ),
        basis_scope="general",
        applicability_notice=(
            "这是该类纠纷的一般规则，是否适用于本案仍需结合交易"
            "关系、具体约定和证据核对。"
        ),
    )


def packet(**updates: object) -> GroundingPacket:
    values: dict[str, object] = {
        "current_message": "我希望商家重新发货",
        "turn_intent": "stated_goal",
        "case_summary": "用户反映商家发错货，当前希望补发正确商品。",
        "confirmed_facts": {},
        "current_goal": "要求商家重新发货",
        "completed_actions": [],
        "coverage_mode": "formal",
        "topic_id": "return_refused",
        "topic_label": "退货换货被拒",
        "formal_findings": [],
        "allowed_actions": [
            "通过平台聊天向商家发送补发要求，写明订单号、错发商品、"
            "应补发商品和答复期限，并保存发送及已读记录。",
            "商家逾期不回复时，通过订单售后或平台客服提交投诉。",
        ],
        "evidence_targets": [
            "保存订单详情、实际收到商品、快递面单和外包装的照片。",
        ],
        "verified_statutes": [general_statute()],
        "limitations": [
            "当前只确认一般合同履行规则，具体责任仍需结合订单和沟通记录核对。",
        ],
        "previously_answered": [],
        "one_allowed_next_question": None,
        "direct_answer_draft": (
            "可以把补发正确商品作为当前诉求，并通过平台聊天明确提出。"
        ),
    }
    values.update(updates)
    return GroundingPacket.model_validate(values)


def test_grounding_packet_is_frozen_and_bounded() -> None:
    value = packet()

    with pytest.raises(ValidationError):
        value.current_goal = "改成退款"  # type: ignore[misc]


def test_general_basis_mapping_is_explicit_and_does_not_guess() -> None:
    assert general_basis_refs("service_contract", "会员服务没履行") == (
        "民法典.第五百零九条",
        "民法典.第五百七十七条",
        "消费者权益保护法.第三十九条",
    )
    assert general_basis_refs("logistics_travel_food", "外卖有异物") == (
        "食品安全法.第一百四十八条",
        "消费者权益保护法.第三十九条",
        "民法典.第五百七十七条",
    )
    assert general_basis_refs("logistics_travel_food", "快递包裹丢失") == (
        "消费者权益保护法.第三十九条",
        "民法典.第五百七十七条",
    )
    assert general_basis_refs("general_rental", "房东一直不维修") == (
        "民法典.第五百零九条",
        "民法典.第七百一十三条",
    )
    assert general_basis_refs("general_rental", "房东突然涨租") == (
        "民法典.第五百零九条",
    )
    assert general_basis_refs("privacy_reputation", "有人造谣") == (
        "民法典.第一千零二十四条",
    )
    assert general_basis_refs("unknown", "随便一件事") == ()


@pytest.mark.parametrize(
    ("topic_id", "message", "expected"),
    [
        (
            "education_minor_safety",
            "孩子被同学校园欺凌，学校没有处理",
            (
                "民法典.第一千一百九十九条",
                "民法典.第一千二百条",
                "民法典.第一千二百零一条",
            ),
        ),
        (
            "medical_service_dispute",
            "医院拒绝提供与纠纷有关的病历",
            (
                "民法典.第一千二百二十五条",
                "民法典.第一千二百二十二条",
            ),
        ),
        (
            "medical_service_dispute",
            "手术前没有告知医疗风险，后来造成损害",
            (
                "民法典.第一千二百一十八条",
                "民法典.第一千二百一十九条",
            ),
        ),
        (
            "traffic_accident",
            "交通事故中有人受伤并住院",
            (
                "民法典.第一千二百零八条",
                "民法典.第一千一百七十九条",
            ),
        ),
        (
            "personal_injury",
            "我在商场摔伤并支付了医疗费",
            (
                "民法典.第一千一百九十八条",
                "民法典.第一千一百七十九条",
            ),
        ),
        (
            "personal_injury",
            "我被邻居饲养的狗咬伤",
            (
                "民法典.第一千二百四十五条",
                "民法典.第一千一百七十九条",
            ),
        ),
        (
            "workplace_harassment",
            "领导利用从属关系对我实施性骚扰",
            (
                "民法典.第一千零一十条",
                "劳动争议调解仲裁法.第二条",
                "劳动争议调解仲裁法.第六条",
            ),
        ),
        (
            "debt_collection",
            "朋友借钱不还，我有借条",
            (
                "民法典.第六百六十七条",
                "民法典.第六百七十五条",
            ),
        ),
        (
            "debt_collection",
            "民间借贷约定了很高的利息",
            (
                "民法典.第六百六十七条",
                "民法典.第六百七十五条",
                "民法典.第六百八十条",
            ),
        ),
        (
            "debt_collection",
            "催收人员爆通讯录并持续骚扰联系人",
            (
                "民法典.第九百九十五条",
                "民法典.第一千零三十二条",
            ),
        ),
        (
            "property_neighbor",
            "楼上漏水，物业一直不处理",
            (
                "民法典.第九百四十二条",
                "民法典.第二百八十八条",
            ),
        ),
        (
            "privacy_reputation",
            "对方公开我的个人信息并造谣",
            (
                "民法典.第一千零二十四条",
                "民法典.第一千零三十四条",
                "民法典.第一千零三十五条",
            ),
        ),
        (
            "family_support_property",
            "对方不付抚养费，也不让我探望孩子",
            (
                "民法典.第一千零六十七条",
                "民法典.第一千零八十六条",
            ),
        ),
        (
            "game_account_dispute",
            "游戏平台把我的账号封禁了",
            (
                "民法典.第五百零九条",
                "消费者权益保护法.第三十九条",
            ),
        ),
        (
            "payment_fraud",
            "有人冒充客服骗我转账",
            (
                "民法典.第一千一百六十五条",
                "消费者权益保护法.第四十四条",
                "消费者权益保护法.第五十五条",
            ),
        ),
    ],
)
def test_general_basis_mapping_selects_only_relevant_verified_law(
    topic_id: str,
    message: str,
    expected: tuple[str, ...],
) -> None:
    assert general_basis_refs(topic_id, message) == expected


def test_local_answer_confirms_replacement_goal_with_concrete_action() -> None:
    draft = build_local_answer(packet())

    assert "补发" in draft.direct_reply
    assert "不必先改成退款" in draft.direct_reply
    assert "如果还没正式提出" in draft.direct_reply
    assert any("订单号" in action and "答复期限" in action for action in draft.actions)
    assert draft.next_question is None
    assert draft.used_statute_ids == ["民法典.第五百七十七条"]


def test_local_privacy_answer_directly_confirms_delete_request() -> None:
    value = packet(
        current_message="对方还在公开个人信息，我先要求删除可以吗？",
        turn_intent="question",
        case_summary="对方持续公开用户个人信息。",
        current_goal="要求删除公开的个人信息并停止传播",
        coverage_mode="unverified_guidance",
        topic_id="privacy_reputation",
        topic_label="隐私、个人信息、名誉和网络骚扰",
        direct_answer_draft=None,
    )

    draft = build_local_answer(value)

    assert "可以先要求删除" in draft.direct_reply
    assert "先完整保存" in draft.direct_reply
    assert "向平台举报" in draft.direct_reply


def test_local_school_answer_escalates_persistent_nonresponse() -> None:
    value = packet(
        current_message="是同学欺凌，学校一直不处理，我接下来怎么办？",
        turn_intent="question",
        case_summary="孩子在学校被同学欺凌，学校未处理。",
        current_goal="要求学校保护孩子并处理欺凌事件",
        coverage_mode="unverified_guidance",
        topic_id="education_minor_safety",
        topic_label="教育、未成年人和校园安全",
        direct_answer_draft=None,
    )

    draft = build_local_answer(value)

    assert "不要只停留在口头催促" in draft.direct_reply
    assert "采取保护措施" in draft.direct_reply
    assert "教育主管部门" in draft.direct_reply


def test_food_amount_answer_explains_rule_without_automatic_promise() -> None:
    value = packet(
        current_message="30元外卖里有虫子，我已经拍照但吃掉一部分，可以要求多少",
        turn_intent="question",
        case_summary="用户反映30元外卖中有虫子，已拍照并吃掉一部分。",
        current_goal="确定可以提出的金额诉求",
        coverage_mode="unverified_guidance",
        topic_id="logistics_travel_food",
        topic_label="物流、旅游与食品消费",
        allowed_actions=[
            "保存订单、付款记录和照片，并通过平台工单向商家提出书面诉求。",
        ],
        evidence_targets=[
            "保留原始照片、订单、付款记录、包装和与商家的完整沟通记录。",
        ],
        verified_statutes=[],
        one_allowed_next_question="目前是否仍保留包装或剩余食品？",
        direct_answer_draft=None,
    )

    draft = build_local_answer(value)

    assert "吃掉一部分" in draft.direct_reply
    assert "不等于" in draft.direct_reply
    assert "实际损失" in draft.direct_reply
    assert "价款十倍" in draft.direct_reply
    assert "损失三倍" in draft.direct_reply
    assert "一千元" in draft.direct_reply
    assert "自动成立" in draft.direct_reply
    assert "30元" not in draft.direct_reply
    assert draft.used_statute_ids == []


def test_non_food_logistics_amount_does_not_receive_food_compensation() -> None:
    value = packet(
        current_message="快递包裹丢失了，可以要求多少赔偿",
        turn_intent="question",
        case_summary="用户反映快递包裹丢失，询问赔偿金额。",
        current_goal="确定快递丢失的赔偿诉求",
        coverage_mode="unverified_guidance",
        topic_id="logistics_travel_food",
        topic_label="物流、旅游与食品消费",
        verified_statutes=[],
        direct_answer_draft="先按包裹价值和能够证明的实际损失提出书面诉求。",
    )

    draft = build_local_answer(value)

    assert "包裹价值" in draft.direct_reply
    assert "价款十倍" not in draft.direct_reply
    assert "损失三倍" not in draft.direct_reply
    assert "一千元" not in draft.direct_reply


def test_local_answer_replaces_unanchored_draft_for_deposit_damage() -> None:
    value = packet(
        current_message="房东说墙面有划痕，要扣全部押金，我该怎么回应？",
        turn_intent="question",
        case_summary="房东以墙面划痕为由要求扣除全部押金。",
        current_goal="反对无依据地扣除全部押金",
        topic_id="deposit_deduction",
        topic_label="租房押金扣减",
        direct_answer_draft=(
            "目前信息不足，不能直接判断结论，需要进一步核对情况。"
        ),
    )

    draft = build_local_answer(value)

    assert "墙面划痕" in draft.direct_reply
    assert "不等于可以直接扣除全部押金" in draft.direct_reply
    assert "实际费用和对应凭证" in draft.direct_reply
    assert "入住与退租时的照片" in draft.direct_reply
    assert "目前信息不足" not in draft.direct_reply


def test_food_safety_statute_uses_specific_applicability_notice() -> None:
    statute = StatuteRecord(
        id=1,
        law_name="中华人民共和国食品安全法",
        law_short="食品安全法",
        article_no="第一百四十八条",
        article_num=148,
        content="第一百四十八条　用于测试的已核验正文。",
        chapter="第九章　法律责任",
        effective_date="2025-12-01",
        source_url=(
            "https://flk.npc.gov.cn/detail?id="
            "7b5a76d0461745a08d3f964916b87ef3"
        ),
    )

    grounded = GroundingStatute.from_statute(
        statute,
        basis_scope="general",
    )

    assert "不符合食品安全标准" in grounded.applicability_notice
    assert "经营者明知" in grounded.applicability_notice
    assert "不等于自动适用" in grounded.applicability_notice
    assert "完整沟通记录" in grounded.applicability_notice


def test_valid_composition_can_only_select_locked_content() -> None:
    source = packet()
    draft = build_local_answer(source)
    composition = GroundedAnswerComposition(
        direct_reply="可以把补发正确商品作为当前诉求，并通过平台聊天留痕。",
        actions=source.allowed_actions,
        evidence=source.evidence_targets,
        legal_explanation=draft.legal_explanation,
        limitations=source.limitations,
        next_question=None,
        used_statute_ids=["民法典.第五百七十七条"],
        provider="fake",
        model="fake-deterministic-v1",
    )

    merged = merge_grounded_answer(source, draft, composition)

    assert merged.direct_reply == composition.direct_reply
    assert merged.actions == source.allowed_actions
    assert merged.used_statute_ids == ["民法典.第五百七十七条"]


@pytest.mark.parametrize(
    "updates",
    [
        {"used_statute_ids": ["消费者权益保护法.第五十五条"]},
        {"next_question": "你还想退款还是补发？"},
        {"actions": ["直接去法院起诉并保证胜诉。"]},
        {"direct_reply": "详见 https://example.com"},
        {"direct_reply": "依据第五十五条，你一定能获得三倍赔偿。"},
    ],
)
def test_composition_cannot_expand_grounding_packet(
    updates: dict[str, object],
) -> None:
    source = packet()
    draft = build_local_answer(source)
    values: dict[str, object] = {
        "direct_reply": draft.direct_reply,
        "actions": source.allowed_actions,
        "evidence": source.evidence_targets,
        "legal_explanation": draft.legal_explanation,
        "limitations": source.limitations,
        "next_question": source.one_allowed_next_question,
        "used_statute_ids": ["民法典.第五百七十七条"],
        "provider": "fake",
        "model": "fake-deterministic-v1",
    }
    values.update(updates)
    composition = GroundedAnswerComposition.model_validate(values)

    with pytest.raises(ValueError):
        merge_grounded_answer(source, draft, composition)


LETTER_DRAFT = (
    "您好，我想书面反映一项与“退货换货被拒”相关的情况。"
    "我的当前请求是：请补发正确商品。"
    "请确认收到，并以书面方式告知处理联系人和下一步安排。"
)
LETTER_RECIPIENT = "与事件直接相关、能够登记和处理问题的负责人"


def letter_packet(**updates: object) -> GroundingPacket:
    values: dict[str, object] = {
        "letter_recipient": LETTER_RECIPIENT,
        "letter_objective": "确认收到情况说明，并告知处理联系人和下一步安排",
        "letter_draft": LETTER_DRAFT,
    }
    values.update(updates)
    return packet(**values)


def letter_composition(
    source: GroundingPacket,
    letter_body: str | None,
) -> GroundedAnswerComposition:
    draft = build_local_answer(source)
    return GroundedAnswerComposition(
        direct_reply=draft.direct_reply,
        actions=source.allowed_actions,
        evidence=source.evidence_targets,
        legal_explanation=draft.legal_explanation,
        limitations=source.limitations,
        next_question=source.one_allowed_next_question,
        used_statute_ids=["民法典.第五百七十七条"],
        letter_body=letter_body,
        provider="fake",
        model="fake-deterministic-v1",
    )


def test_local_answer_carries_letter_draft_from_packet() -> None:
    assert build_local_answer(letter_packet()).letter_body == LETTER_DRAFT
    assert build_local_answer(packet()).letter_body is None


def test_unmodified_letter_template_passes_validation() -> None:
    """模板正文必须能通过校验。

    收件人字段是角色描述而非称呼，模板正文本身也不含那串字，
    早期版本据此比对会把未改动的模板一并拒掉。
    """
    source = letter_packet()
    draft = build_local_answer(source)

    merged = merge_grounded_answer(
        source,
        draft,
        letter_composition(source, LETTER_DRAFT),
    )

    assert merged.letter_body == LETTER_DRAFT


def test_letter_may_be_rewritten_for_the_specific_case() -> None:
    source = letter_packet()
    draft = build_local_answer(source)
    rewritten = (
        "您好，我是这笔订单的买家。收到的商品与下单商品不一致，"
        "希望尽快补发正确商品。请确认收到，并以书面方式告知"
        "处理联系人和下一步安排。"
    )

    merged = merge_grounded_answer(
        source,
        draft,
        letter_composition(source, rewritten),
    )

    assert merged.letter_body == rewritten


@pytest.mark.parametrize(
    "letter_body",
    [
        # 代对方许诺义务：对方并未作出承诺。
        "您好，关于这笔订单，您必须全额退款。请确认收到。",
        # 限期加威胁：把沟通信写成催告函。
        "您好，请于三日内退款，否则我将起诉。请确认收到。",
        # 替用户放弃权利。
        "您好，我放弃主张其他赔偿，只要补发。请确认收到。",
        # 混入依据包之外的第三方机构。
        "您好，我已向市场监管部门举报此事。请确认收到。",
        # 正文内引用法条条号。
        "您好，根据《民法典》相关规定，请补发正确商品。请确认收到。",
    ],
)
def test_letter_rejects_overreaching_rewrites(letter_body: str) -> None:
    source = letter_packet()
    draft = build_local_answer(source)

    with pytest.raises(ValueError):
        merge_grounded_answer(
            source,
            draft,
            letter_composition(source, letter_body),
        )


def test_letter_requires_a_draft_in_the_packet() -> None:
    """依据包没给草稿时，模型不得凭空生成正文。"""
    source = packet()
    draft = build_local_answer(source)

    with pytest.raises(ValueError):
        merge_grounded_answer(
            source,
            draft,
            letter_composition(source, LETTER_DRAFT),
        )


def test_missing_letter_body_falls_back_to_no_letter() -> None:
    source = letter_packet()
    draft = build_local_answer(source)

    for empty in (None, "   "):
        merged = merge_grounded_answer(
            source,
            draft,
            letter_composition(source, empty),
        )
        assert merged.letter_body is None


def test_second_stage_is_used_for_contextual_or_legal_answer() -> None:
    source = packet()
    draft = build_local_answer(source)

    assert should_compose_grounded_answer(source, draft, is_followup=True)
    assert not should_compose_grounded_answer(
        source.model_copy(
            update={
                "coverage_mode": "emergency_guidance",
                "verified_statutes": [],
            }
        ),
        draft.model_copy(update={"used_statute_ids": []}),
        is_followup=False,
    )
