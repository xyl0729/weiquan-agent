"""食品安全与医疗纠纷的分流回归。

`_TOPIC_TRIGGERS` 里医疗组的「医院」排在食品组之前，而顺序即优先级，
所以「外卖吃完上吐下泻进医院了」原本被判成医疗纠纷。但去医院是食物
中毒案情的自然归宿，不等于跟医院有纠纷——争的是那顿饭，被告是商家，
依据该是食品安全法第一百四十八条(十倍赔偿)而不是民法典的诊疗规则。

修法是共现判定而不是调顺序:真正的医疗纠纷也常提到吃药、不舒服，
把食品组整体提前会反过来劫走医疗案情。所以两个方向都要测。
"""

from __future__ import annotations

import pytest

from app.agent.grounding import general_basis_refs
from app.retrieval.expansion import infer_topic


@pytest.mark.parametrize(
    "message",
    [
        "外卖吃完上吐下泻进医院了",
        "点的菜里有虫子，吃完拉肚子",
        "奶茶喝了肚子疼了一整天",
        "饭店吃的东西不干净，食物中毒住院",
    ],
)
def test_food_illness_is_food_safety_even_when_hospital_is_mentioned(
    message: str,
) -> None:
    assert infer_topic(message) == "logistics_travel_food"


@pytest.mark.parametrize(
    "message",
    [
        "医院不肯给我复印病历",
        "医生开的药吃了不舒服，说是误诊",
        "手术之后一直恢复不好，医院不认",
        "医院开的药太贵了，我觉得是乱收费",
        # 有就医词也有不适词，但没有食品来源词——仍是医疗纠纷。
        # 共现判定必须要求「食品来源」在场，否则会反向劫走这类案情。
        "在医院吃了他们开的药就上吐下泻",
    ],
)
def test_medical_dispute_is_not_hijacked_by_food_cooccurrence(
    message: str,
) -> None:
    assert infer_topic(message) == "medical_service_dispute"


def test_food_safety_route_yields_the_ten_fold_damages_basis() -> None:
    """分流正确才拿得到对用户最有力的那条依据。

    食品安全法第一百四十八条是十倍赔偿；走医疗纠纷路径拿到的是诊疗
    损害规则，答不了「能赔多少」。
    """
    topic = infer_topic("外卖吃完上吐下泻进医院了")
    refs = general_basis_refs(topic, "外卖吃完上吐下泻进医院了")

    assert "食品安全法.第一百四十八条" in refs
