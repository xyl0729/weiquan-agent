from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.retrieval.database import (
    StatuteRecord,
    connect_database,
    get_statute_by_ref,
    lexical_tokens,
)


@dataclass(frozen=True, slots=True)
class SearchHit:
    statute: StatuteRecord
    score: float
    forced: bool


def build_match_query(query: str) -> str:
    tokens = lexical_tokens(query)
    return " OR ".join(f'"{token}"' for token in tokens)


def _record_from_row(row: sqlite3.Row) -> StatuteRecord:
    return StatuteRecord(
        id=int(row["id"]),
        law_name=str(row["law_name"]),
        law_short=str(row["law_short"]),
        article_no=str(row["article_no"]),
        article_num=int(row["article_num"]),
        content=str(row["content"]),
        chapter=str(row["chapter"]) if row["chapter"] is not None else None,
        effective_date=str(row["effective_date"]),
        source_url=str(row["source_url"]),
    )


def search_connection(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int = 5,
    forced_refs: Iterable[str] = (),
) -> list[SearchHit]:
    if limit < 1:
        raise ValueError("limit 必须大于 0")

    hits: list[SearchHit] = []
    seen_ids: set[int] = set()

    for ref in dict.fromkeys(forced_refs):
        statute = get_statute_by_ref(connection, ref)
        if statute is None:
            raise LookupError(f"强制召回引用未命中: {ref}")
        hits.append(SearchHit(statute=statute, score=float("inf"), forced=True))
        seen_ids.add(statute.id)

    remaining = max(0, limit - len(hits))
    match_query = build_match_query(query)
    if remaining == 0 or not match_query:
        return hits

    rows = connection.execute(
        """
        SELECT s.id, s.law_name, s.law_short, s.article_no, s.article_num,
               s.content, s.chapter, s.effective_date, s.source_url,
               bm25(statutes_fts, 1.0, 2.0, 1.5) AS rank
        FROM statutes_fts
        JOIN statutes AS s ON s.id = statutes_fts.rowid
        WHERE statutes_fts MATCH ?
        ORDER BY rank ASC, s.article_num ASC
        LIMIT ?
        """,
        (match_query, limit + len(seen_ids)),
    ).fetchall()

    ranked_added = 0
    for row in rows:
        statute = _record_from_row(row)
        if statute.id in seen_ids:
            continue
        hits.append(
            SearchHit(
                statute=statute,
                score=-float(row["rank"]),
                forced=False,
            )
        )
        seen_ids.add(statute.id)
        ranked_added += 1
        if ranked_added >= remaining:
            break
    return hits


def search_statutes(
    database_path: Path,
    query: str,
    *,
    limit: int = 5,
    forced_refs: Iterable[str] = (),
) -> list[SearchHit]:
    connection = connect_database(database_path)
    try:
        return search_connection(
            connection,
            query,
            limit=limit,
            forced_refs=forced_refs,
        )
    finally:
        connection.close()
