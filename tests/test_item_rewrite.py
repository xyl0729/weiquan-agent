# -*- coding: utf-8 -*-
"""动作与证据条目的改写边界。

原先 merge_grounded_answer 用 issubset 校验这两个字段，模型只能整条
保留或整条删除，不能按案情改写。结果是不论用户描述甲醛还是欠薪，
动作和证据都是逐字相同的模板句——这是「模糊不清、像模板」的主要来源。

现在允许改写措辞，但每条都必须大部分内容源自同一条已批准条目。
这些断言把边界固定下来：正常改写要放行，凭空新增和升级途径要拦住。
"""

from __future__ import annotations

import pytest

from app.agent.grounding import _validate_rewritten_items


APPROVED = [
    "通过可留痕渠道向房东说明甲醛超标情况、当前诉求和希望回复的时间。",
    "保存检测报告、就医记录和全部沟通内容，便于后续核对。",
]


@pytest.mark.parametrize(
    "item",
    [
        # 原样保留必须放行，否则模型不改写时整轮成文作废。
        "通过可留痕渠道向房东说明甲醛超标情况、当前诉求和希望回复的时间。",
        # 补入渠道和诉求细节：这正是本次改动要支持的核心场景。
        "通过微信或短信等可留痕渠道向房东说明甲醛超标情况、"
        "我要求整治的诉求和希望回复的时间。",
        # 调整语序和称谓。
        "先保存检测报告和就医记录，以及全部沟通内容，便于后续核对。",
        # 把案情事实写进证据条目。
        "保存甲醛检测报告、头痛咳嗽的就医记录和全部沟通内容，便于后续核对。",
    ],
)
def test_case_specific_rewrite_is_allowed(item: str) -> None:
    _validate_rewritten_items([item], APPROVED, label="动作")


def test_invented_action_is_rejected() -> None:
    """依据包没批准的动作不得出现，哪怕听起来合理。

    「重新做通风处理」在甲醛案里是很自然的建议，但它不在依据包里，
    放行就等于让模型自己决定用户该做什么。
    """
    with pytest.raises(ValueError, match="偏离"):
        _validate_rewritten_items(
            ["立即联系装修公司重新做一次全屋通风处理并索取发票。"],
            APPROVED,
            label="动作",
        )


def test_escalation_authority_is_rejected() -> None:
    """机构升级途径不得混进当前步骤。

    向监管部门举报属于 escalation 字段的后续路径。混进当前动作会让
    用户以为现在就该去举报，而依据包并没有批准这一步。
    """
    with pytest.raises(ValueError, match="升级途径"):
        _validate_rewritten_items(
            [
                "通过可留痕渠道向房东说明甲醛超标情况，"
                "同时向市场监管部门举报。"
            ],
            APPROVED,
            label="动作",
        )


def test_authority_marker_allowed_when_packet_approves_it() -> None:
    """依据包本身批准了升级途径时不该拦。

    劳动争议这类主题的批准动作里就含仲裁，此时提及仲裁是照做，
    不是越界。
    """
    approved = [
        "整理劳动合同、工资记录和考勤材料，向劳动人事争议仲裁"
        "委员会申请仲裁。",
    ]
    _validate_rewritten_items(
        [
            "整理劳动合同、近三个月工资记录和考勤材料，"
            "向劳动人事争议仲裁委员会申请仲裁。"
        ],
        approved,
        label="动作",
    )


@pytest.mark.parametrize(
    "items",
    [
        ["今天天气不错，适合出门散步。"],
        ["   "],
        [],
    ],
)
def test_empty_or_unrelated_items_are_rejected(items: list[str]) -> None:
    with pytest.raises(ValueError):
        _validate_rewritten_items(items, APPROVED, label="证据")


def test_rejection_names_the_field() -> None:
    """报错信息要指明是哪个字段，否则排查时分不清动作还是证据。"""
    with pytest.raises(ValueError, match="证据"):
        _validate_rewritten_items(
            ["完全无关的一句话，和依据包没有关系。"],
            APPROVED,
            label="证据",
        )
