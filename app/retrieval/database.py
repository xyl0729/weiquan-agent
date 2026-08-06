from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.retrieval.schema import SeedBundle


SCHEMA_VERSION = 1
_TEXT_RUN = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+")

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE statutes (
    id INTEGER PRIMARY KEY,
    law_name TEXT NOT NULL,
    law_short TEXT NOT NULL,
    article_no TEXT NOT NULL,
    article_num INTEGER NOT NULL,
    content TEXT NOT NULL,
    chapter TEXT,
    effective_date TEXT NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE(law_name, article_no),
    UNIQUE(law_short, article_no)
);

CREATE VIRTUAL TABLE statutes_fts USING fts5(
    content,
    law_short,
    article_no,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class StatuteRecord:
    id: int
    law_name: str
    law_short: str
    article_no: str
    article_num: int
    content: str
    chapter: str | None
    effective_date: str
    source_url: str

    @property
    def ref(self) -> str:
        return f"{self.law_short}.{self.article_no}"


def lexical_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).lower()
    tokens: list[str] = []
    for match in _TEXT_RUN.finditer(normalized):
        run = match.group(0)
        if run.isascii():
            tokens.append(run)
            continue

        if len(run) == 1:
            tokens.append(run)
            continue

        for width in (2, 3):
            if len(run) >= width:
                tokens.extend(
                    run[index : index + width]
                    for index in range(len(run) - width + 1)
                )
        if len(run) <= 8:
            tokens.append(run)

    return list(dict.fromkeys(tokens))


def fts_document(value: str) -> str:
    return " ".join(lexical_tokens(value))


def parse_ref(ref: str) -> tuple[str, str]:
    law_short, separator, article_no = ref.strip().partition(".")
    if not separator or not law_short.strip() or not article_no.strip():
        raise ValueError(f"无效法条引用，预期格式为 法律简称.条号: {ref!r}")
    if "." in article_no:
        raise ValueError(f"无效法条引用，只允许一个分隔点: {ref!r}")
    return law_short.strip(), article_no.strip()


def connect_database(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"法条数据库不存在，请先执行入库脚本: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def rebuild_database(bundle: SeedBundle, database_path: Path) -> int:
    database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = database_path.with_name(f"{database_path.name}.building")

    if temporary_path.exists():
        temporary_path.unlink()

    connection = sqlite3.connect(temporary_path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        for statute in bundle.statutes:
            cursor = connection.execute(
                """
                INSERT INTO statutes (
                    law_name, law_short, article_no, article_num, content,
                    chapter, effective_date, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    statute.law_name,
                    statute.law_short,
                    statute.article_no,
                    statute.article_num,
                    statute.content,
                    statute.chapter,
                    statute.effective_date.isoformat(),
                    str(statute.source_url),
                ),
            )
            connection.execute(
                """
                INSERT INTO statutes_fts(rowid, content, law_short, article_no)
                VALUES (?, ?, ?, ?)
                """,
                (
                    cursor.lastrowid,
                    fts_document(statute.content),
                    fts_document(statute.law_short),
                    fts_document(statute.article_no),
                ),
            )

        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "placeholder_data": str(bundle.placeholder_data).lower(),
            "warning": bundle.warning,
            "statute_count": str(len(bundle.statutes)),
            "ingested_at": datetime.now(UTC).isoformat(),
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            metadata.items(),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        if temporary_path.exists():
            temporary_path.unlink()
        raise
    else:
        connection.close()

    os.replace(temporary_path, database_path)
    return len(bundle.statutes)


def get_statute_by_ref(
    connection: sqlite3.Connection,
    ref: str,
) -> StatuteRecord | None:
    law_short, article_no = parse_ref(ref)
    row = connection.execute(
        """
        SELECT id, law_name, law_short, article_no, article_num, content,
               chapter, effective_date, source_url
        FROM statutes
        WHERE law_short = ? AND article_no = ?
        """,
        (law_short, article_no),
    ).fetchone()
    return StatuteRecord(**dict(row)) if row else None


def get_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}

