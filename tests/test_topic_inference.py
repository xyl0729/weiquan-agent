# -*- coding: utf-8 -*-
"""主题推断：精选映射没覆盖的问题也要拿到正确条文。

甲醛这类问题此前 topic_id 落到 unknown，general_basis_refs 返回空，
整轮咨询一条法律依据都没有。改成按口语触发词推断主题后，条文仍由
人工精选映射决定，不让 BM25 直接挑条文——实测检索分数无法区分条文
立场，甲醛案里对用户不利的第七百一十一条拿到 50.19 分，而正确的
第七百一十三条只有 25.46。

这里锁住触发词优先级。优先级搞错会给出方向相反的条文，比没有条文
更糟，因此每条断言都对应一个曾经真实出错的案情。
"""

from __future__ import annotations

import pytest

from app.agent.grounding import general_basis_refs
from app.retrieval.expansion import infer_topic


@pytest.mark.parametrize(
    ("message", "expected_topic"),
    [
        # 伤害动作优先于场所名词：被邻居家的狗咬是动物致害的人身损害，
        # 曾被 property_neighbor 的「邻居」抢走，拿到相邻关系条文。
        ("我被邻居家的狗咬了，狗主人不认账", "personal_injury"),
        ("在超市地面滑倒摔伤了，超市说不关他们的事", "personal_injury"),
        # 租赁关系词优先于场所现象词：房东不修漏水是租赁维修义务问题，
        # 曾被 property_neighbor 的「漏水」抢走。
        ("房东一直不给修漏水", "general_rental"),
        ("我租的房子甲醛超标，住进去以后一直头痛咳嗽", "general_rental"),
        # 场所现象词在没有租赁关系时仍归相邻关系。
        ("楼上漏水把我家天花板泡了，物业不管", "property_neighbor"),
        # 「借了我…不还」不含「借钱」「借款」，曾经完全推断不出主题。
        ("对方借了我五万块一直不还，只有微信记录", "debt_collection"),
        ("我借给朋友三万，他一直拖着不还", "debt_collection"),
        ("外卖里有虫子，商家说不是他们的问题", "logistics_travel_food"),
        ("公司拖欠我三个月工资，还不给交社保", "wage_social_insurance"),
        ("对方在网上造谣说我坏话", "privacy_reputation"),
    ],
)
def test_infer_topic_resolves_colloquial_cases(
    message: str,
    expected_topic: str,
) -> None:
    assert infer_topic(message) == expected_topic


@pytest.mark.parametrize(
    "message",
    ["你好", "今天天气不错", "你是谁开发的", "谢谢"],
)
def test_small_talk_infers_no_topic(message: str) -> None:
    """闲聊推断不出主题，宁可不给依据也不硬凑。"""
    assert infer_topic(message) is None


@pytest.mark.parametrize(
    ("message", "expected_ref"),
    [
        # 甲醛这类居住条件危及健康的情形，正确条文是第七百三十一条
        # （租赁物危及安全或健康时承租人可随时解除合同）。该条已于
        # 2026-08-28 录入法条库，此前只能退而给第七百一十三条
        # （维修义务）——方向对但力度弱，只能要求修不能解约止损。
        ("我租的房子甲醛超标，住进去以后一直头痛咳嗽", "民法典.第七百三十一条"),
        # 纯报修类仍应命中维修义务条：这里要的是"让对方修"，
        # 不是"解除合同"，两类情形不能混为一谈。
        ("房东一直不给修漏水", "民法典.第七百一十三条"),
        ("我被邻居家的狗咬了，狗主人不认账", "民法典.第一千二百四十五条"),
        ("对方借了我五万块一直不还", "民法典.第六百六十七条"),
    ],
)
def test_inferred_topic_yields_expected_statute(
    message: str,
    expected_ref: str,
) -> None:
    """推断出的主题必须能取到该案真正对应的条文。"""
    topic = infer_topic(message)
    assert topic is not None
    refs = general_basis_refs(topic, message)
    assert expected_ref in refs


def test_animal_injury_not_confused_with_lost_pet() -> None:
    """走丢的宠物不是动物致害，不该拿到侵权条文。

    扩展词表最初用裸的「狗」「猫」做触发词，「我的猫走丢了」因此
    命中动物致害条文。触发词收紧为伤害动作后修复。
    """
    assert infer_topic("我的猫走丢了，怎么找回来") != "personal_injury"


@pytest.mark.parametrize(
    ("message", "expected_ref"),
    [
        # 关系认定（第七百零三条）：二房东、口头约定、没签合同这类
        # 案情里，用户第一个被质疑的是「你算不算承租人」。关系立不住
        # 时后面的维修、减租、解约条文都用不上。
        ("我是跟二房东租的，没签合同，现在大房东要赶我走", "民法典.第七百零三条"),
        ("房东口头说租一年，现在不承认我租过", "民法典.第七百零三条"),
        # 租金支付期限（第七百二十一条）：期限没约定或约定不明时的
        # 补充规则，回答「房东能不能突然要我提前交」。
        ("房东突然要我提前交租，之前没约定租金什么时候交", "民法典.第七百二十一条"),
        ("押一付三，房东说交租时间改了", "民法典.第七百二十一条"),
        # 到期继续居住（第七百三十四条第一款）：出租人未提异议的，
        # 原合同继续有效但转为不定期，房东不能以「到期了」为由直接赶人。
        (
            "合同到期了，房东突然让我三天内搬走，我一直住着他也没说过什么",
            "民法典.第七百三十四条",
        ),
        ("租期届满后我继续住，房东现在不让续租", "民法典.第七百三十四条"),
    ],
)
def test_general_rental_wires_relation_term_and_holdover(
    message: str,
    expected_ref: str,
) -> None:
    """703/721/734 曾入库但无任何代码引用，等于白躺在库里。"""
    topic = infer_topic(message)
    assert topic == "general_rental"
    assert expected_ref in general_basis_refs(topic, message)


@pytest.mark.parametrize(
    ("message", "unexpected_ref"),
    [
        # 报修和押金争议不需要回头论证租赁合同成立，无条件给出
        # 第七百零三条只会稀释真正对应案情的条文。
        ("房东一直不给修漏水", "民法典.第七百零三条"),
        ("押金到期不退", "民法典.第七百零三条"),
        # 「押金到期不退」的「到期」指押金退还时点，不是租期届满后
        # 继续居住，不该命中不定期租赁条。
        ("押金到期不退", "民法典.第七百三十四条"),
        # 甲醛案要的是解约权，不是租金支付期限规则。
        ("我租的房子甲醛超标，住进去以后一直头痛咳嗽", "民法典.第七百二十一条"),
    ],
)
def test_general_rental_new_refs_do_not_over_trigger(
    message: str,
    unexpected_ref: str,
) -> None:
    topic = infer_topic(message)
    assert topic == "general_rental"
    assert unexpected_ref not in general_basis_refs(topic, message)
