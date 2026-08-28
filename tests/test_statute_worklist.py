# -*- coding: utf-8 -*-
"""待录入清单生成脚本的行为约束。

这个脚本存在的前提是「法条正文必须人工从全国人大数据库逐条录入核验」。
因此最要紧的断言不是输出格式，而是它绝不碰法条库、绝不产出正文：
一旦脚本能自己抓取、比对并落库，placeholder_data 那道门禁就变成了
自我认证，人工核验只剩形式。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from scripts.statute_worklist import (
    load_wishlist,
    render,
    split_by_presence,
)


def _wishlist(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    path = tmp_path / "wishlist.yaml"
    path.write_text(
        yaml.safe_dump({"statutes": entries}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _entry(ref: str = "民法典.第七百三十一条") -> dict[str, str]:
    return {
        "ref": ref,
        "law_name": "中华人民共和国民法典",
        "source_url": "https://flk.npc.gov.cn/detail?id=test",
        "topic": "general_rental",
        "why": "甲醛超标危及健康的租房纠纷需要本条。",
        "current_fallback": "民法典.第七百一十三条，只能要求维修。",
    }


def test_wishlist_carries_no_statute_text(tmp_path: Path) -> None:
    """清单条目不得包含正文字段。

    带上 content 就等于脚本可以直接产出法条数据，人工录入这一步
    就可以被跳过。字段白名单在 load_wishlist 里，这里从数据侧确认
    真实的 wishlist 文件也没有夹带正文。
    """
    real = Path("data/statute_wishlist.yaml")
    entries = load_wishlist(real)
    for entry in entries:
        assert "content" not in entry
        assert "正文" not in entry


def test_generated_document_contains_no_statute_text() -> None:
    """生成的清单里只有链接和理由，没有条文正文。"""
    entries = load_wishlist(Path("data/statute_wishlist.yaml"))
    document = render(entries, [])
    # 法条正文的典型开头是「第X条　」（全角空格），出现即说明夹带了正文。
    assert "条　" not in document
    assert "flk.npc.gov.cn" in document
    assert "必须人工" in document


def test_already_ingested_entries_are_separated(tmp_path: Path) -> None:
    """已入库的条目要挪到另一组，避免重复录入。"""
    database = tmp_path / "statutes.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE statutes (
            id INTEGER PRIMARY KEY,
            law_name TEXT NOT NULL,
            law_short TEXT NOT NULL,
            article_no TEXT NOT NULL,
            article_num INTEGER NOT NULL,
            content TEXT NOT NULL,
            chapter TEXT,
            effective_date TEXT NOT NULL,
            source_url TEXT NOT NULL
        );
        INSERT INTO statutes VALUES (
            1, '中华人民共和国民法典', '民法典', '第七百一十三条', 713,
            '正文', NULL, '2021-01-01', 'https://example.com'
        );
        """
    )
    connection.commit()
    connection.close()

    entries = [
        _entry("民法典.第七百三十一条"),
        _entry("民法典.第七百一十三条"),
    ]
    pending, done = split_by_presence(entries, database)

    assert [item["ref"] for item in pending] == ["民法典.第七百三十一条"]
    assert [item["ref"] for item in done] == ["民法典.第七百一十三条"]


def test_missing_required_field_is_rejected(tmp_path: Path) -> None:
    """缺少理由或链接的条目要拒绝：没有官方链接就无法核验。"""
    entry = _entry()
    del entry["source_url"]
    with pytest.raises(ValueError, match="缺少字段"):
        load_wishlist(_wishlist(tmp_path, [entry]))


def test_duplicate_ref_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="重复引用"):
        load_wishlist(_wishlist(tmp_path, [_entry(), _entry()]))


def test_malformed_ref_is_rejected(tmp_path: Path) -> None:
    """引用格式必须和检索层一致，否则录入后取不到。"""
    entry = _entry("民法典第七百三十一条")
    with pytest.raises(ValueError):
        load_wishlist(_wishlist(tmp_path, [entry]))


def test_real_wishlist_is_valid() -> None:
    """仓库里的 wishlist 必须始终可解析，否则脚本在真实数据上就跑不通。"""
    entries = load_wishlist(Path("data/statute_wishlist.yaml"))
    assert entries
    refs = [entry["ref"] for entry in entries]
    assert len(refs) == len(set(refs))
    # 甲醛案的正确条文是这份清单存在的最初原因，必须在列。
    assert "民法典.第七百三十一条" in refs
