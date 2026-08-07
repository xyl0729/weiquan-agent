from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.agent.errors import SessionNotFoundError
from app.agent.models import UsageInfo
from app.db.session import SCHEMA_VERSION, SessionStore


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def make_store(path: Path, *, ttl_hours: int = 72) -> SessionStore:
    store = SessionStore(path, ttl_hours=ttl_hours, now=lambda: NOW)
    store.initialize()
    return store


def test_initialize_is_idempotent_and_sets_schema_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    store = make_store(path)

    store.initialize()

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                """
            )
        }
    assert version == SCHEMA_VERSION
    assert {
        "sessions",
        "turns",
        "audit_records",
        "usage_daily",
        "rate_limit_daily",
    }.issubset(tables)


def test_create_restore_and_update_session_slides_ttl(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "app.db", ttl_hours=2)
    session = store.create_session(
        facts={"deposit_amount": 2000},
        jurisdiction="CN",
    )

    assert session.status == "collecting"
    assert session.followup_round == 0
    assert session.expires_at == NOW + timedelta(hours=2)

    later = NOW + timedelta(hours=1)
    updated = store.update_session(
        session.id,
        scenario_id="deposit_deduction",
        facts={"deposit_amount": 2000, "withheld_amount": 1500},
        followup_round=1,
        status="need_more_facts",
        now=later,
    )

    assert updated.scenario_id == "deposit_deduction"
    assert updated.facts["withheld_amount"] == 1500
    assert updated.updated_at == later
    assert updated.expires_at == later + timedelta(hours=2)
    assert store.require_session(session.id, now=later) == updated


@pytest.mark.parametrize("followup_round", [-1, 3])
def test_session_rejects_invalid_followup_round(
    tmp_path: Path,
    followup_round: int,
) -> None:
    store = make_store(tmp_path / "app.db")
    session = store.create_session()

    with pytest.raises(ValueError):
        store.update_session(
            session.id,
            followup_round=followup_round,
        )


def test_expired_session_is_rejected_and_cascades_sensitive_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    store = make_store(path, ttl_hours=1)
    session = store.create_session()
    turn = store.add_turn(
        session.id,
        user_message="房东不退押金",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
    )
    store.add_audit_record(
        session.id,
        turn_id=turn.id,
        stage="extract",
        status="ok",
    )

    after_expiry = NOW + timedelta(hours=1)
    assert store.get_session(session.id, now=after_expiry) is None
    with pytest.raises(SessionNotFoundError):
        store.require_session(session.id, now=after_expiry)

    with sqlite3.connect(path) as connection:
        turn_count = connection.execute(
            "SELECT COUNT(*) FROM turns"
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM audit_records"
        ).fetchone()[0]
    assert turn_count == 0
    assert audit_count == 0


def test_turn_and_audit_round_trip_with_redaction(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "app.db")
    session = store.create_session()
    turn = store.add_turn(
        session.id,
        user_message=(
            "我的 Key 是 sk-abcdefghijklmnop，"
            "Authorization: Bearer sk-qrstuvwxyz123456"
        ),
        facts={"deposit_amount": 2000},
        rule_matches=[{"rule_id": "r01", "matched": True}],
        response={"status": "ready"},
        provider_name="deepseek",
        provider_model="deepseek-chat",
        provider_request_id="request-1",
        usage=UsageInfo(
            input_tokens=10,
            output_tokens=4,
            total_tokens=14,
            estimated_cost_usd=0.001,
        ),
    )
    audit = store.add_audit_record(
        session.id,
        turn_id=turn.id,
        stage="render",
        status="ok",
        playbook_id="deposit_deduction",
        playbook_version="1.0.0",
        citations=["住房租赁条例.第十条"],
    )

    stored_turn = store.list_turns(session.id)[0]
    stored_audit = store.list_audit_records(audit_id=audit.audit_id)[0]

    assert "sk-" not in stored_turn.user_message
    assert stored_turn.user_message.count("[REDACTED]") == 2
    assert stored_turn.usage.estimated_cost_usd == pytest.approx(0.001)
    assert stored_audit.turn_id == turn.id
    assert stored_audit.citations == ["住房租赁条例.第十条"]


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "secret"},
        {"nested": {"Authorization": "Bearer secret"}},
        {"items": [{"raw_prompt": "private"}]},
        {"request": {"headers": {"x": "y"}}},
    ],
)
def test_structured_sensitive_fields_are_never_persisted(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    store = make_store(tmp_path / "app.db")

    with pytest.raises(ValueError, match="敏感字段"):
        store.create_session(facts=payload)


def test_usage_and_rate_limit_accumulate_by_utc_day(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "app.db")

    first = store.record_usage(
        client_hash="client-hash",
        provider="deepseek",
        usage=UsageInfo(
            input_tokens=100,
            output_tokens=25,
            estimated_cost_usd=0.002,
        ),
    )
    second = store.record_usage(
        client_hash="client-hash",
        provider="deepseek",
        usage=UsageInfo(input_tokens=50, output_tokens=10),
    )
    rate_one = store.increment_rate_limit(client_hash="client-hash")
    rate_two = store.increment_rate_limit(client_hash="client-hash")

    assert first.request_count == 1
    assert second.request_count == 2
    assert second.input_tokens == 150
    assert second.output_tokens == 35
    assert second.total_tokens == 185
    assert second.priced_request_count == 1
    assert second.estimated_cost_usd == pytest.approx(0.002)
    assert store.daily_estimated_cost(
        date(2026, 8, 6),
        provider="deepseek",
    ) == pytest.approx(0.002)
    assert rate_one.request_count == 1
    assert rate_two.request_count == 2


def test_rate_increment_is_atomic_under_threads(tmp_path: Path) -> None:
    store = make_store(tmp_path / "app.db")

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(
            executor.map(
                lambda _: store.increment_rate_limit(
                    client_hash="concurrent-client"
                ),
                range(20),
            )
        )

    assert max(record.request_count for record in records) == 20
    stored = store.get_rate_limit(
        day=NOW.date(),
        client_hash="concurrent-client",
    )
    assert stored is not None
    assert stored.request_count == 20


def test_list_sessions_excludes_zero_turns_and_orders_by_update(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "app.db")
    first = store.create_session(now=NOW)
    store.update_session(
        first.id,
        status="need_more_facts",
        now=NOW,
    )
    store.add_turn(
        first.id,
        user_message="第一条咨询",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
        now=NOW,
    )

    later = NOW + timedelta(minutes=5)
    second = store.create_session(now=later)
    store.update_session(
        second.id,
        status="ready",
        now=later,
    )
    store.add_turn(
        second.id,
        user_message="第二条咨询",
        facts={},
        rule_matches=[],
        response={"status": "ready"},
        now=later,
    )
    store.create_session(now=later)

    sessions = store.list_sessions(now=later)

    assert [item.id for item in sessions] == [second.id, first.id]
    assert sessions[0].first_user_message == "第二条咨询"


def test_get_history_and_delete_session_are_atomic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    store = make_store(path)
    session = store.create_session()
    store.update_session(session.id, status="ready")
    turn = store.add_turn(
        session.id,
        user_message="房东无理由扣除押金",
        facts={},
        rule_matches=[],
        response={"status": "ready"},
    )
    store.add_audit_record(
        session.id,
        turn_id=turn.id,
        stage="response",
        status="ok",
    )

    history = store.get_session_history(session.id)

    assert history is not None
    restored_session, restored_turns = history
    assert restored_session.id == session.id
    assert [item.id for item in restored_turns] == [turn.id]
    assert store.delete_session(session.id) is True
    assert store.delete_session(session.id) is False
    assert store.get_session_history(session.id) is None

    with sqlite3.connect(path) as connection:
        turn_count = connection.execute(
            "SELECT COUNT(*) FROM turns"
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM audit_records"
        ).fetchone()[0]
    assert turn_count == 0
    assert audit_count == 0


def test_list_sessions_purges_expired_records(tmp_path: Path) -> None:
    store = make_store(tmp_path / "app.db", ttl_hours=1)
    session = store.create_session()
    store.update_session(session.id, status="need_more_facts")
    store.add_turn(
        session.id,
        user_message="一条会过期的咨询",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
    )

    assert store.list_sessions(
        now=NOW + timedelta(hours=1)
    ) == []
