"""未识别主题的档案分桶回归。

背景：`_PROFILES` 里有 16 套人工写好的主题档案，键与 `infer_topic` 的
返回值域、`GENERAL_BASIS_REFS` 的键完全一一对应。但 `build()` 原来只看
`coverage.topic_id`，路由定不出主题时一律落到通用兜底档案——甲醛案就是
这样：依据侧靠 `infer_topic` 兜底认出了租赁纠纷，文案侧却仍在用「与事件
直接相关的负责人」这种谁都能套的说法。这组测试锁住三件事：

1. 认不出主题时用消息推断，落到已有档案；
2. 路由已经定出主题时不被触发词覆盖；
3. 推不出来时仍回到通用兜底，不瞎猜。
"""

from __future__ import annotations

import pytest

from app.agent.guidance import (
    _PROFILES,
    _UNKNOWN_TOPIC,
    GuidanceBuilder,
    _select_profile,
)
from app.agent.models import CoverageResult
from app.retrieval.expansion import infer_topic


def coverage(topic_id: str) -> CoverageResult:
    return CoverageResult(
        mode="unverified_guidance",
        topic_id=topic_id,
        topic_label=topic_id,
        confidence=0.9,
        playbook_id=None,
        notice="当前仅提供安全与信息整理指导。",
        risk_flags=[],
    )


def profile_key(profile: object) -> str:
    for key, value in _PROFILES.items():
        if value is profile:
            return key
    raise AssertionError("档案不在 _PROFILES 中")


def test_profile_keys_cover_every_inferable_topic() -> None:
    """档案键必须盖住 infer_topic 的全部返回值。

    少一个键就意味着那个主题推断出来也用不上，会静默退回兜底档案——
    这正是本次要修的问题，所以把对应关系锁成测试而不是靠人记。
    """
    from app.retrieval.expansion import _TOPIC_TRIGGERS

    inferable = {topic_id for topic_id, _ in _TOPIC_TRIGGERS}
    assert inferable <= set(_PROFILES) - {_UNKNOWN_TOPIC}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("甲醛超标住进去头痛咳嗽", "general_rental"),
        ("房东一直不给修漏水", "general_rental"),
        ("房子到期了房东不让我住", "general_rental"),
        ("公司拖欠工资三个月没发", "wage_social_insurance"),
        ("被邻居家的狗咬了", "personal_injury"),
        ("健身房跑路了不退钱", "service_contract"),
        ("快递把我的东西弄丢了", "logistics_travel_food"),
    ],
)
def test_unknown_topic_falls_back_to_inferred_profile(
    message: str,
    expected: str,
) -> None:
    selected = _select_profile(coverage(_UNKNOWN_TOPIC), message)

    assert profile_key(selected) == expected
    assert selected is not _PROFILES[_UNKNOWN_TOPIC]


def test_unknown_is_a_profile_key_so_presence_check_is_not_enough() -> None:
    """守住第一版踩过的坑。

    `"unknown"` 本身就是 `_PROFILES` 的键，所以 `get(topic_id) is not
    None` 这种写法会在 topic_id="unknown" 时直接命中兜底档案返回，
    `infer_topic` 永远不被调用。当时 904 个测试全绿，改动却一点没生效，
    是靠实测探针才发现的。这条断言把「兜底键存在」和「推断被跳过」
    分开锁住。
    """
    assert _UNKNOWN_TOPIC in _PROFILES

    message = "甲醛超标住进去头痛咳嗽"
    assert infer_topic(message) == "general_rental"
    assert _PROFILES.get(_UNKNOWN_TOPIC) is not None
    # 兜底键存在，但推断仍须发生。
    assert profile_key(_select_profile(coverage(_UNKNOWN_TOPIC), message)) == (
        "general_rental"
    )


def test_routed_topic_is_not_overridden_by_triggers() -> None:
    """路由已经定出主题时，触发词不得改档案。

    路由看的是整轮上下文，单句触发词看不全；让触发词覆盖路由结论，
    等于用更弱的信号推翻更强的信号。
    """
    selected = _select_profile(coverage("service_contract"), "甲醛超标")

    assert profile_key(selected) == "service_contract"


@pytest.mark.parametrize("message", ["", "完全没有任何线索的一句话"])
def test_uninferable_message_keeps_generic_profile(message: str) -> None:
    selected = _select_profile(coverage(_UNKNOWN_TOPIC), message)

    assert selected is _PROFILES[_UNKNOWN_TOPIC]


def test_build_threads_message_into_profile_choice() -> None:
    """端到端确认 message 真的被 build() 用上了。

    只测 `_select_profile` 不够——参数没接进 `build()` 的话，单元测试
    照样全绿而线上文案不变。
    """
    builder = GuidanceBuilder()
    generic = builder.build(coverage(_UNKNOWN_TOPIC), facts={})
    bucketed = builder.build(
        coverage(_UNKNOWN_TOPIC),
        facts={},
        message="甲醛超标住进去头痛咳嗽",
    )

    assert generic.communication_guide.recipient != (
        bucketed.communication_guide.recipient
    )
    assert bucketed.communication_guide.recipient == (
        _PROFILES["general_rental"].recipient
    )


def test_build_unverified_stage_threads_message_into_profile_choice() -> None:
    builder = GuidanceBuilder()
    generic = builder.build_unverified_stage(
        coverage(_UNKNOWN_TOPIC),
        stage=1,
    )
    bucketed = builder.build_unverified_stage(
        coverage(_UNKNOWN_TOPIC),
        stage=1,
        message="甲醛超标住进去头痛咳嗽",
    )

    assert generic.action != bucketed.action
    assert bucketed.action == _PROFILES["general_rental"].actions[0]
