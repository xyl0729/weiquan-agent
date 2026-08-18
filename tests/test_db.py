from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.agent.errors import SessionNotFoundError
from app.agent.models import UsageInfo
from app.attachments.models import ExtractionBlock, ExtractionResult
from app.attachments.store import AttachmentStore
from app.db.contracts import LOCAL_DEVELOPMENT_OWNER_ID
from app.db.session import BASE_SCHEMA_SQL, SCHEMA_VERSION, SessionStore


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def make_store(path: Path, *, ttl_hours: int = 72) -> SessionStore:
    store = SessionStore(path, ttl_hours=ttl_hours, now=lambda: NOW)
    store.initialize()
    return store


def confirmed_attachment(
    store: AttachmentStore,
    *,
    name: str,
    text: str,
):
    processing = store.create_processing(
        original_name=name,
        media_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
    )
    store.save_extraction(
        processing.id,
        ExtractionResult(
            media_type="application/pdf",
            page_count=1,
            extraction_method="direct_text",
            blocks=(
                ExtractionBlock(
                    page_number=1,
                    block_index=0,
                    text=text,
                    confidence=1,
                ),
            ),
        ),
    )
    return store.confirm(processing.id, text)


def bind_attachment(
    store: AttachmentStore,
    *,
    session_id: str,
    turn_id: str,
    attachment_id: str,
) -> None:
    reservation_id = store.reserve([attachment_id])
    store.bind_reserved(
        reservation_id,
        session_id=session_id,
        turn_id=turn_id,
        expected_ids=[attachment_id],
    )


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
        "attachments",
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
    assert history.session.id == session.id
    assert [item.turn.id for item in history.turns] == [turn.id]
    assert history.turns[0].attachments == ()
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


def test_session_delete_and_expiry_only_cascade_owned_attachments(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    store = make_store(path, ttl_hours=1)
    attachments = AttachmentStore(
        store,
        draft_ttl_seconds=3 * 3600,
        now=lambda: NOW,
    )

    first_session = store.create_session()
    first_turn = store.add_turn(
        first_session.id,
        user_message="第一个案件",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
    )
    first_attachment = confirmed_attachment(
        attachments,
        name="第一个案件.pdf",
        text="第一份正文",
    )
    bind_attachment(
        attachments,
        session_id=first_session.id,
        turn_id=first_turn.id,
        attachment_id=first_attachment.id,
    )

    second_session = store.create_session()
    second_turn = store.add_turn(
        second_session.id,
        user_message="第二个案件",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
    )
    second_attachment = confirmed_attachment(
        attachments,
        name="第二个案件.pdf",
        text="第二份正文",
    )
    bind_attachment(
        attachments,
        session_id=second_session.id,
        turn_id=second_turn.id,
        attachment_id=second_attachment.id,
    )
    draft = attachments.create_processing(
        original_name="未绑定草稿.pdf",
        media_type="application/pdf",
        size_bytes=128,
        sha256="b" * 64,
    )

    assert store.delete_session(first_session.id) is True
    assert attachments.get_optional(first_attachment.id) is None
    assert attachments.get_optional(second_attachment.id) is not None
    assert attachments.get_optional(draft.id) is not None
    assert [turn.id for turn in store.list_turns(second_session.id)] == [
        second_turn.id
    ]

    assert store.get_session_history(
        second_session.id,
        now=NOW + timedelta(hours=1),
    ) is None
    assert attachments.get_optional(second_attachment.id) is None
    assert attachments.get_optional(draft.id, now=NOW) is not None

    with sqlite3.connect(path) as connection:
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM attachments"
            )
        }
        remaining_turns = {
            row[0] for row in connection.execute("SELECT id FROM turns")
        }
    assert remaining == {draft.id}
    assert first_turn.id not in remaining_turns
    assert second_turn.id not in remaining_turns


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


def test_real_v1_database_migrates_without_rewriting_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    session_id = "11111111-1111-4111-8111-111111111111"
    turn_id = "22222222-2222-4222-8222-222222222222"
    with sqlite3.connect(path) as connection:
        connection.executescript(BASE_SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO sessions (
                id, scenario_id, facts_json, followup_round, status,
                jurisdiction, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                "deposit_deduction",
                '{"deposit_amount":2000}',
                1,
                "need_more_facts",
                "CN",
                NOW.isoformat(),
                NOW.isoformat(),
                (NOW + timedelta(hours=72)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO turns (
                id, session_id, user_message, facts_json,
                rule_matches_json, response_json, provider_name,
                provider_model, provider_request_id, input_tokens,
                output_tokens, total_tokens, estimated_cost_usd,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id,
                session_id,
                "房东扣了押金",
                '{"deposit_amount":2000}',
                "[]",
                '{"status":"need_more_facts"}',
                "fake",
                "fake-local",
                None,
                10,
                2,
                12,
                None,
                NOW.isoformat(),
            ),
        )
        before_session = connection.execute(
            """
            SELECT id, scenario_id, facts_json, followup_round, status,
                   jurisdiction, created_at, updated_at, expires_at
            FROM sessions
            """
        ).fetchone()
        before_turn = connection.execute(
            """
            SELECT id, session_id, user_message, facts_json,
                   rule_matches_json, response_json, provider_name,
                   provider_model, provider_request_id, input_tokens,
                   output_tokens, total_tokens, estimated_cost_usd,
                   created_at
            FROM turns
            """
        ).fetchone()
        connection.execute("PRAGMA user_version = 1")

    SessionStore(path, now=lambda: NOW).initialize()

    with sqlite3.connect(path) as connection:
        after_session = connection.execute(
            """
            SELECT id, scenario_id, facts_json, followup_round, status,
                   jurisdiction, created_at, updated_at, expires_at
            FROM sessions
            """
        ).fetchone()
        after_turn = connection.execute(
            """
            SELECT id, session_id, user_message, facts_json,
                   rule_matches_json, response_json, provider_name,
                   provider_model, provider_request_id, input_tokens,
                   output_tokens, total_tokens, estimated_cost_usd,
                   created_at
            FROM turns
            """
        ).fetchone()
        owners = {
            row[0]
            for row in connection.execute(
                """
                SELECT owner_id FROM sessions
                UNION SELECT owner_id FROM turns
                """
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        attachment_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(attachments)"
            )
        }
    assert after_session == before_session
    assert after_turn == before_turn
    assert version == SCHEMA_VERSION
    assert owners == {LOCAL_DEVELOPMENT_OWNER_ID}
    assert {
        "reservation_id",
        "reserved_at",
        "turn_position",
    }.issubset(attachment_columns)


def test_failed_v1_migration_keeps_original_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(BASE_SCHEMA_SQL)
        connection.execute(
            "CREATE TABLE attachments (id TEXT PRIMARY KEY)"
        )
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(sqlite3.Error):
        SessionStore(path, now=lambda: NOW).initialize()

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(attachments)"
            )
        ]
    assert version == 1
    assert columns == ["id"]


def test_database_from_future_version_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute(
            "INSERT INTO marker (value) VALUES ('keep-me')"
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="版本"):
        SessionStore(path, now=lambda: NOW).initialize()

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        marker = connection.execute(
            "SELECT value FROM marker"
        ).fetchone()[0]
    assert version == SCHEMA_VERSION + 1
    assert marker == "keep-me"
