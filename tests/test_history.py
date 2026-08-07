from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.errors import DataIntegrityError
from app.db.session import SessionStore
from app.history.service import SessionHistoryService, history_title


def _public_response(
    session_id: str,
    turn_id: str,
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "audit_id": str(uuid4()),
        "followup_round": 1,
        "can_ask_more": True,
        "status": "need_more_facts",
        "verdict": None,
        "plan": None,
        "questions": ["请补充合同约定"],
        "limitations": [],
        "citations": [],
        "usage": {
            "provider": "fake",
            "model": "fake-extractor-v1",
            "request_id": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": None,
        },
    }


def test_history_title_normalizes_whitespace_and_unicode() -> None:
    assert history_title("  房东\n\n扣押金  ") == "房东 扣押金"

    source = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥天地玄黄"

    assert history_title(source) == f"{source[:24]}…"


def test_history_service_lists_and_restores_public_turns(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "app.db")
    store.initialize()
    session = store.create_session()
    turn_id = str(uuid4())
    store.update_session(
        session.id,
        scenario_id="deposit_deduction",
        status="need_more_facts",
    )
    store.add_turn(
        session.id,
        turn_id=turn_id,
        user_message="房东扣了押金",
        facts={"private_fact": "not public"},
        rule_matches=[],
        response=_public_response(session.id, turn_id),
    )
    service = SessionHistoryService(store)

    listed = service.list_sessions()
    detail = service.get_session(session.id)

    assert listed[0].title == "房东扣了押金"
    assert listed[0].scenario_id == "deposit_deduction"
    assert detail.session == listed[0]
    assert detail.turns[0].response.status == "need_more_facts"
    assert not hasattr(detail.session, "facts")


def test_history_service_revalidates_stored_public_response(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    store = SessionStore(path)
    store.initialize()
    session = store.create_session()
    store.update_session(session.id, status="need_more_facts")
    turn = store.add_turn(
        session.id,
        user_message="房东扣了押金",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE turns SET response_json = ? WHERE id = ?",
            ('{"status":"ready"}', turn.id),
        )

    with pytest.raises(
        DataIntegrityError,
        match="历史咨询数据未通过完整性检查",
    ):
        SessionHistoryService(store).get_session(session.id)
