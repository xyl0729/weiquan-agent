"""`semantic_confident` 下限的校准结果回归。

这道门只在一种情形下生效:模型给出了具体主题，但该主题的关键词对这条
消息一个都没命中——也就是「要不要在关键词帮不上忙时仍然采信模型」。

阈值 0.55 由 71 条真实抽取校准得出(见 routing.py 常量处的注释与
scripts/calibrate_routing_threshold*.py)。两簇实测分布:

    模型弃权         n=30   0.1 ~ 0.5
    模型下判断       n=23   0.6 ~ 0.9

本文件把这两簇的边界钉住。改动阈值前请先重跑校准脚本,不要凭感觉调
——原值 0.80 就是没校准的猜测,误拒了 43% 的正确判断。
"""

from __future__ import annotations

import pytest

from app.agent.routing import _SEMANTIC_CONFIDENCE_FLOOR

# 实测两簇的边界值。改这两个数就等于宣称重新校准过。
_VAGUE_CLUSTER_MAX = 0.5
_COMMITTED_CLUSTER_MIN = 0.6


def test_floor_sits_in_the_measured_gap() -> None:
    """阈值必须落在两簇之间的空隙里，不能落进任一簇内部。"""
    assert _VAGUE_CLUSTER_MAX < _SEMANTIC_CONFIDENCE_FLOOR
    assert _SEMANTIC_CONFIDENCE_FLOOR < _COMMITTED_CLUSTER_MIN


def test_floor_is_the_gap_midpoint() -> None:
    """取中点而不是贴边，两侧各留一半余量。

    贴着 0.6 会让簇 B 的下界样本擦边通过，模型稍有波动就掉出去；
    贴着 0.5 则把含糊簇的上界样本放进来。
    """
    midpoint = (_VAGUE_CLUSTER_MAX + _COMMITTED_CLUSTER_MIN) / 2

    assert _SEMANTIC_CONFIDENCE_FLOOR == pytest.approx(midpoint)


@pytest.mark.parametrize(
    "confidence",
    [0.1, 0.2, 0.3, 0.5],
)
def test_vague_cluster_stays_below_floor(confidence: float) -> None:
    """模型弃权时的 confidence 实测值都不该通过这道门。"""
    assert confidence < _SEMANTIC_CONFIDENCE_FLOOR


@pytest.mark.parametrize(
    "confidence",
    [0.6, 0.7, 0.8, 0.9],
)
def test_committed_cluster_passes_floor(confidence: float) -> None:
    """模型真正下判断时的 confidence 实测值都该通过这道门。

    0.6 那条是甲醛案的口语表述(「住进去就头痛咳嗽，屋里味儿特别冲」，
    模型给 general_rental，正确)。原阈值 0.80 恰好把它拒了，而
    infer_topic 对这句返回 None——两道兜底同时失效，用户一条法条
    都拿不到。这条断言就是钉住这个案子。
    """
    assert confidence >= _SEMANTIC_CONFIDENCE_FLOOR


def test_old_uncalibrated_value_would_reject_the_formaldehyde_case() -> None:
    """记录原值 0.80 的实际代价，防止有人凭「更保险」改回去。

    0.80 并不更保险:它挡不住唯一一条实测答错的样本(confidence 0.7，
    落在正确簇正中间)，只是把 43% 的正确判断一起拒了。
    """
    formaldehyde_colloquial = 0.6
    misclassified_but_confident = 0.7

    assert formaldehyde_colloquial < 0.80  # 正确判断，被旧阈值误拒
    assert misclassified_but_confident < 0.80  # 错误判断，旧阈值也挡不住
    # 新阈值放行正确的那条；错的那条任何阈值都分不开，见注释。
    assert formaldehyde_colloquial >= _SEMANTIC_CONFIDENCE_FLOOR
