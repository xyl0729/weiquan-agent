from pathlib import Path

import pytest

from app.retrieval.benchmark import (
    DataGateError,
    RecallBenchmark,
    assert_data_gate_ready,
    evaluate_recall_at_5,
)
from app.retrieval.database import rebuild_database
from app.retrieval.schema import load_seed_bundle
from app.retrieval.search import build_match_query, search_statutes


THREE_STATUTES = """
placeholder_data: true
warning: 测试占位数据
statutes:
  - law_name: 测试法（占位数据）
    law_short: 测试法
    article_no: 第一条
    article_num: 1
    content: 占位文本，押金退还和租赁纠纷，不代表真实法律规定。
    effective_date: 1970-01-01
    source_url: https://example.invalid/1
  - law_name: 测试法（占位数据）
    law_short: 测试法
    article_no: 第二条
    article_num: 2
    content: 占位文本，预付卡退款和商家停业，不代表真实法律规定。
    effective_date: 1970-01-01
    source_url: https://example.invalid/2
  - law_name: 测试法（占位数据）
    law_short: 测试法
    article_no: 第三条
    article_num: 3
    content: 占位文本，加班工资和劳动争议，不代表真实法律规定。
    effective_date: 1970-01-01
    source_url: https://example.invalid/3
"""


def prepare_database(tmp_path: Path) -> Path:
    seed_path = tmp_path / "seed.yaml"
    seed_path.write_text(THREE_STATUTES, encoding="utf-8")
    database_path = tmp_path / "statutes.db"
    rebuild_database(load_seed_bundle(seed_path), database_path)
    return database_path


def test_match_query_keeps_exact_two_character_legal_term() -> None:
    query = build_match_query("押金不退")

    assert '"押金"' in query
    assert '"不退"' in query


def test_fts_search_handles_colloquial_chinese(tmp_path: Path) -> None:
    database_path = prepare_database(tmp_path)

    hits = search_statutes(database_path, "健身房预付卡不肯退款", limit=5)

    assert hits
    assert hits[0].statute.ref == "测试法.第二条"
    assert hits[0].forced is False


def test_forced_refs_are_first_and_deduplicated(tmp_path: Path) -> None:
    database_path = prepare_database(tmp_path)

    hits = search_statutes(
        database_path,
        "押金不退",
        limit=5,
        forced_refs=["测试法.第三条", "测试法.第三条"],
    )

    assert hits[0].statute.ref == "测试法.第三条"
    assert hits[0].forced is True
    assert [hit.statute.ref for hit in hits].count("测试法.第三条") == 1


def test_missing_forced_ref_fails_loudly(tmp_path: Path) -> None:
    database_path = prepare_database(tmp_path)

    with pytest.raises(LookupError, match="强制召回引用未命中"):
        search_statutes(
            database_path,
            "押金不退",
            forced_refs=["测试法.第九条"],
        )


def test_recall_calculation_uses_macro_average(tmp_path: Path) -> None:
    database_path = prepare_database(tmp_path)
    benchmark = RecallBenchmark.model_validate(
        {
            "placeholder_data": True,
            "warning": "测试",
            "minimum_cases": 50,
            "minimum_recall_at_5": 0.9,
            "cases": [
                {
                    "id": "deposit",
                    "query": "押金不退",
                    "expected_refs": ["测试法.第一条"],
                },
                {
                    "id": "unknown",
                    "query": "完全无关词汇",
                    "expected_refs": ["测试法.第三条"],
                },
            ],
        }
    )

    report = evaluate_recall_at_5(benchmark, database_path)

    assert report.case_count == 2
    assert report.passed_cases == 1
    assert report.recall_at_5 == 0.5
    assert report.missed == {"unknown": ["测试法.第三条"]}


def test_placeholder_data_cannot_pass_gate(tmp_path: Path) -> None:
    database_path = prepare_database(tmp_path)
    benchmark = RecallBenchmark.model_validate(
        {
            "placeholder_data": True,
            "warning": "测试",
            "minimum_cases": 50,
            "minimum_recall_at_5": 0.9,
            "cases": [],
        }
    )

    with pytest.raises(DataGateError) as exc_info:
        assert_data_gate_ready(benchmark, database_path)

    message = str(exc_info.value)
    assert "占位数据" in message
    assert "至少需要 50 条" in message

