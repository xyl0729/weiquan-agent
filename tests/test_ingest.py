from pathlib import Path

import pytest
from pydantic import ValidationError

from app.retrieval.database import (
    connect_database,
    fts_document,
    get_metadata,
    get_statute_by_ref,
    lexical_tokens,
    rebuild_database,
)
from app.retrieval.schema import SeedBundle, load_seed_bundle


VALID_SEED = """
placeholder_data: true
warning: 明显占位数据
statutes:
  - law_name: 测试法（占位数据）
    law_short: 测试法
    article_no: 第一条
    article_num: 1
    content: 占位文本，检验押金退还检索，不代表真实法律规定。
    chapter: 测试章
    effective_date: 1970-01-01
    source_url: https://example.invalid/article-1
"""


def write_seed(tmp_path: Path, content: str = VALID_SEED) -> Path:
    path = tmp_path / "seed.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_seed_requires_every_field(tmp_path: Path) -> None:
    invalid = VALID_SEED.replace(
        "    content: 占位文本，检验押金退还检索，不代表真实法律规定。\n",
        "",
    )

    with pytest.raises(ValidationError):
        load_seed_bundle(write_seed(tmp_path, invalid))


def test_seed_rejects_duplicate_statutes(tmp_path: Path) -> None:
    first_record = VALID_SEED.split("statutes:\n", maxsplit=1)[1]
    duplicated = VALID_SEED + first_record

    with pytest.raises(ValidationError, match="法条记录重复"):
        load_seed_bundle(write_seed(tmp_path, duplicated))


def test_placeholder_bundle_requires_warning(tmp_path: Path) -> None:
    invalid = VALID_SEED.replace("warning: 明显占位数据", "warning: ''")

    with pytest.raises(ValidationError, match="warning"):
        load_seed_bundle(write_seed(tmp_path, invalid))


def test_rebuild_database_creates_statutes_fts_and_metadata(
    tmp_path: Path,
) -> None:
    bundle = load_seed_bundle(write_seed(tmp_path))
    database_path = tmp_path / "statutes.db"

    count = rebuild_database(bundle, database_path)

    assert count == 1
    connection = connect_database(database_path)
    try:
        statute = get_statute_by_ref(connection, "测试法.第一条")
        metadata = get_metadata(connection)
        fts_count = connection.execute(
            "SELECT count(*) FROM statutes_fts"
        ).fetchone()[0]
    finally:
        connection.close()

    assert statute is not None
    assert statute.article_num == 1
    assert statute.source_url == "https://example.invalid/article-1"
    assert metadata["placeholder_data"] == "true"
    assert metadata["statute_count"] == "1"
    assert fts_count == 1


def test_chinese_lexical_tokens_keep_two_character_terms() -> None:
    tokens = lexical_tokens("押金退还")

    assert "押金" in tokens
    assert "退还" in tokens
    assert "押金退" in tokens
    assert "押 金" not in fts_document("押金")


def test_unknown_ref_returns_none(tmp_path: Path) -> None:
    bundle = SeedBundle.model_validate(
        load_seed_bundle(write_seed(tmp_path)).model_dump()
    )
    database_path = tmp_path / "statutes.db"
    rebuild_database(bundle, database_path)

    connection = connect_database(database_path)
    try:
        assert get_statute_by_ref(connection, "测试法.第九条") is None
    finally:
        connection.close()

