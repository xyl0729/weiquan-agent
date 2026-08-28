"""检索兜底：精选映射没覆盖的问题也要拿到法条。

甲醛这类问题此前 topic_id 落到 unknown，general_basis_refs 返回空，
整轮咨询一条法律依据都没有。这里断言兜底路径确实补上了条文，
并且不会给闲聊类消息硬凑依据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_pipeline import make_pipeline, run


@pytest.mark.parametrize(
    "message",
    [
        "我租的房子甲醛超标，住进去以后一直头痛咳嗽",
        "楼上漏水把我家天花板泡了，找了几次都没人处理",
        "我被邻居家的狗咬了，狗主人不认账",
    ],
)
def test_uncovered_topics_still_receive_legal_basis(
    tmp_path: Path,
    message: str,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    result = run(pipeline.consult(message=message))

    assert result.reply_citations, "精选映射未覆盖时也应给出法条"
    assert all(
        citation.basis_scope == "general"
        for citation in result.reply_citations
    )
    # 兜底条文同样要带适用性提示，避免被当成本案结论。
    assert all(
        citation.applicability_notice
        for citation in result.reply_citations
    )
    # 兜底条文必须来自已核验法条库，正文非空且带官方链接。
    assert all(citation.content for citation in result.reply_citations)
    assert all(citation.source_url for citation in result.reply_citations)


@pytest.mark.parametrize(
    "message",
    [
        "你好",
        "今天天气不错",
        "你是谁开发的",
    ],
)
def test_small_talk_does_not_get_invented_legal_basis(
    tmp_path: Path,
    message: str,
) -> None:
    """扩展词表没命中就不检索，闲聊不该被硬配法条。"""
    pipeline, _ = make_pipeline(tmp_path)

    result = run(pipeline.consult(message=message))

    assert result.reply_citations == ()
