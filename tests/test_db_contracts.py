from __future__ import annotations

import inspect
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from app.agent.errors import (
    CaseNoProgressError,
    ConsultationConflictError,
)
from app.agent.models import UsageInfo
from app.agent.progression import comparison_units, project_response
from app.attachments.errors import AttachmentNotFoundError
from app.attachments.models import ExtractionBlock, ExtractionResult
from app.attachments.store import AttachmentStore
from app.db.contracts import (
    LOCAL_DEVELOPMENT_OWNER_ID,
    AttachmentBindingCommand,
    ConsultationCommitCommand,
    ConsultationUnitOfWork,
    SessionUpdateCommand,
    TurnWriteCommand,
)
from app.db.session import (
    ATTACHMENT_SCHEMA_SQL,
    BASE_SCHEMA_SQL,
    SCHEMA_VERSION,
    SessionStore,
)
from app.db.tables import (
    consultation_attachments,
    consultation_sessions,
    consultation_turns,
    content_audit_records,
    metadata,
    users,
)


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"


def _store(path: Path) -> SessionStore:
    store = SessionStore(path, now=lambda: NOW)
    store.initialize()
    return store


def _confirmed_attachment(
    attachments: AttachmentStore,
    *,
    owner_id: str,
) -> str:
    record = attachments.create_processing(
        owner_id=owner_id,
        original_name="证据.pdf",
        media_type="application/pdf",
        size_bytes=128,
        sha256="a" * 64,
    )
    attachments.save_extraction(
        record.id,
        ExtractionResult(
            media_type="application/pdf",
            page_count=1,
            extraction_method="direct_text",
            blocks=(
                ExtractionBlock(
                    page_number=1,
                    block_index=0,
                    text="订单金额 299 元",
                    confidence=1,
                ),
            ),
        ),
        owner_id=owner_id,
    )
    return attachments.confirm(
        record.id,
        "订单金额 299 元",
        owner_id=owner_id,
    ).id


def _commit(
    *,
    session_id: str,
    turn_id: str,
    owner_id: str,
    binding: AttachmentBindingCommand | None = None,
    response: dict | None = None,
    expected_latest_turn_id: str | None = None,
    guarded: bool = False,
    scenario_id: str | None = "deposit_deduction",
    facts: dict | None = None,
) -> ConsultationCommitCommand:
    safe_facts = (
        {"deposit_amount": 2000}
        if facts is None
        else facts
    )
    safe_response = response or {
        "turn_kind": "followup_answer",
        "reply": {
            "text": "先保存扣款依据。",
            "suggested_actions": ["书面要求房东说明扣款依据"],
        },
    }
    return ConsultationCommitCommand(
        owner_id=owner_id,
        session_id=session_id,
        session=SessionUpdateCommand(
            scenario_id=scenario_id,
            facts=safe_facts,
            followup_round=1,
            status="need_more_facts",
            jurisdiction="CN",
        ),
        turn=TurnWriteCommand(
            turn_id=turn_id,
            user_message="房东扣了押金",
            facts=safe_facts,
            rule_matches=(),
            response=safe_response,
            provider_name="fake",
            provider_model="fake-local",
            provider_request_id=None,
            usage=UsageInfo(
                input_tokens=10,
                output_tokens=2,
                total_tokens=12,
            ),
        ),
        attachment_binding=binding,
        occurred_at=NOW,
        expected_latest_turn_id=expected_latest_turn_id,
        comparison_units=(
            comparison_units(project_response(safe_response))
            if guarded
            else ()
        ),
    )


def test_persistence_entrypoint_accepts_only_a_typed_command() -> None:
    parameters = tuple(
        inspect.signature(SessionStore.persist_session_turn).parameters
    )

    assert parameters == ("self", "command")
    assert isinstance(
        _store,
        object,
    )
    assert isinstance(
        SessionStore(Path("contract-only.db")),
        ConsultationUnitOfWork,
    )


def test_owner_is_required_on_records_and_scopes_resource_queries(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "app.db")
    first = store.create_session(owner_id=OWNER_A)
    second = store.create_session(owner_id=OWNER_B)

    store.persist_session_turn(
        _commit(
            session_id=first.id,
            turn_id=str(uuid4()),
            owner_id=OWNER_A,
        )
    )
    store.persist_session_turn(
        _commit(
            session_id=second.id,
            turn_id=str(uuid4()),
            owner_id=OWNER_B,
        )
    )

    assert first.owner_id == OWNER_A
    assert store.get_session(first.id, owner_id=OWNER_B) is None
    assert [item.owner_id for item in store.list_sessions(
        owner_id=OWNER_A
    )] == [OWNER_A]
    assert all(
        turn.owner_id == OWNER_A
        for turn in store.list_turns(first.id, owner_id=OWNER_A)
    )


def test_attachment_queries_require_matching_owner(tmp_path: Path) -> None:
    store = _store(tmp_path / "app.db")
    attachments = AttachmentStore(store, now=lambda: NOW)
    attachment_id = _confirmed_attachment(
        attachments,
        owner_id=OWNER_A,
    )

    assert attachments.get(
        attachment_id,
        owner_id=OWNER_A,
    ).owner_id == OWNER_A
    with pytest.raises(AttachmentNotFoundError):
        attachments.get(attachment_id, owner_id=OWNER_B)


def test_typed_commit_atomically_updates_session_turn_and_binding(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "app.db")
    attachments = AttachmentStore(store, now=lambda: NOW)
    session = store.create_session(owner_id=OWNER_A)
    attachment_id = _confirmed_attachment(
        attachments,
        owner_id=OWNER_A,
    )
    reservation_id = attachments.reserve(
        [attachment_id],
        owner_id=OWNER_A,
    )
    turn_id = str(uuid4())

    turn = store.persist_session_turn(
        _commit(
            session_id=session.id,
            turn_id=turn_id,
            owner_id=OWNER_A,
            binding=AttachmentBindingCommand(
                reservation_id=reservation_id,
                attachment_ids=(attachment_id,),
            ),
        )
    )

    restored = store.require_session(session.id, owner_id=OWNER_A)
    bound = attachments.get(attachment_id, owner_id=OWNER_A)
    assert turn.owner_id == OWNER_A
    assert restored.scenario_id == "deposit_deduction"
    assert bound.status == "bound"
    assert bound.turn_id == turn_id
    with pytest.raises(Exception):
        attachments.reserve([attachment_id], owner_id=OWNER_A)


def test_binding_failure_rolls_back_every_write(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    store = _store(path)
    attachments = AttachmentStore(store, now=lambda: NOW)
    session = store.create_session(owner_id=OWNER_A)
    attachment_id = _confirmed_attachment(
        attachments,
        owner_id=OWNER_A,
    )
    reservation_id = attachments.reserve(
        [attachment_id],
        owner_id=OWNER_A,
    )
    turn_id = str(uuid4())
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_attachment_binding
            BEFORE UPDATE OF status ON attachments
            WHEN NEW.status = 'bound'
            BEGIN
                SELECT RAISE(ABORT, 'injected binding failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.persist_session_turn(
            _commit(
                session_id=session.id,
                turn_id=turn_id,
                owner_id=OWNER_A,
                binding=AttachmentBindingCommand(
                    reservation_id=reservation_id,
                    attachment_ids=(attachment_id,),
                ),
            )
        )

    restored_session = store.require_session(
        session.id,
        owner_id=OWNER_A,
    )
    restored_attachment = attachments.get(
        attachment_id,
        owner_id=OWNER_A,
    )
    assert restored_session.scenario_id is None
    assert restored_session.facts == {}
    assert store.list_turns(session.id, owner_id=OWNER_A) == []
    assert restored_attachment.status == "confirmed"
    assert restored_attachment.reservation_id == reservation_id
    assert restored_attachment.turn_id is None


def test_equivalent_concurrent_commits_create_only_one_turn(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "app.db")
    session = store.create_session(owner_id=OWNER_A)
    barrier = Barrier(2)

    def persist(turn_id: str) -> str:
        barrier.wait(timeout=5)
        try:
            store.persist_session_turn(
                _commit(
                    session_id=session.id,
                    turn_id=turn_id,
                    owner_id=OWNER_A,
                    guarded=True,
                )
            )
            return "committed"
        except CaseNoProgressError:
            return "no_progress"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(persist, (str(uuid4()), str(uuid4())))
        )

    assert sorted(outcomes) == ["committed", "no_progress"]
    assert len(store.list_turns(session.id, owner_id=OWNER_A)) == 1


def test_stale_different_commit_rolls_back_session_and_attachment(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "app.db")
    attachments = AttachmentStore(store, now=lambda: NOW)
    session = store.create_session(owner_id=OWNER_A)
    first_turn_id = str(uuid4())
    store.persist_session_turn(
        _commit(
            session_id=session.id,
            turn_id=first_turn_id,
            owner_id=OWNER_A,
            guarded=True,
        )
    )
    attachment_id = _confirmed_attachment(
        attachments,
        owner_id=OWNER_A,
    )
    reservation_id = attachments.reserve(
        [attachment_id],
        owner_id=OWNER_A,
    )
    different_response = {
        "turn_kind": "followup_answer",
        "reply": {
            "text": "下一步核对租赁合同中的押金条款。",
            "suggested_actions": ["核对租赁合同"],
        },
    }

    with pytest.raises(ConsultationConflictError):
        store.persist_session_turn(
            _commit(
                session_id=session.id,
                turn_id=str(uuid4()),
                owner_id=OWNER_A,
                binding=AttachmentBindingCommand(
                    reservation_id=reservation_id,
                    attachment_ids=(attachment_id,),
                ),
                response=different_response,
                expected_latest_turn_id=str(uuid4()),
                guarded=True,
                scenario_id="return_refused",
                facts={"purchase_amount": 999},
            )
        )

    restored = store.require_session(session.id, owner_id=OWNER_A)
    attachment = attachments.get(attachment_id, owner_id=OWNER_A)
    assert restored.scenario_id == "deposit_deduction"
    assert restored.facts == {"deposit_amount": 2000}
    assert [
        turn.id
        for turn in store.list_turns(session.id, owner_id=OWNER_A)
    ] == [first_turn_id]
    assert attachment.status == "confirmed"
    assert attachment.reservation_id == reservation_id
    assert attachment.session_id is None
    assert attachment.turn_id is None


def test_matching_latest_id_still_rejects_duplicate_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "app.db")
    session = store.create_session(owner_id=OWNER_A)
    first_turn_id = str(uuid4())
    response = {
        "turn_kind": "followup_answer",
        "reply": {
            "text": "先保存扣款依据。",
            "suggested_actions": ["书面要求房东说明扣款依据"],
        },
    }
    store.persist_session_turn(
        _commit(
            session_id=session.id,
            turn_id=first_turn_id,
            owner_id=OWNER_A,
            response=response,
            guarded=True,
        )
    )

    with pytest.raises(CaseNoProgressError):
        store.persist_session_turn(
            _commit(
                session_id=session.id,
                turn_id=str(uuid4()),
                owner_id=OWNER_A,
                response=response,
                expected_latest_turn_id=first_turn_id,
                guarded=True,
            )
        )

    assert len(store.list_turns(session.id, owner_id=OWNER_A)) == 1


def test_v2_rows_are_backfilled_to_the_local_owner(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    session_id = "33333333-3333-4333-8333-333333333333"
    turn_id = "44444444-4444-4444-8444-444444444444"
    attachment_id = "55555555-5555-4555-8555-555555555555"
    audit_id = "66666666-6666-4666-8666-666666666666"
    with sqlite3.connect(path) as connection:
        connection.executescript(BASE_SCHEMA_SQL)
        connection.executescript(ATTACHMENT_SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO sessions (
                id, scenario_id, facts_json, followup_round, status,
                jurisdiction, created_at, updated_at, expires_at
            ) VALUES (?, NULL, '{}', 0, 'collecting', NULL, ?, ?, ?)
            """,
            (
                session_id,
                NOW.isoformat(),
                NOW.isoformat(),
                (NOW + timedelta(hours=72)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO turns (
                id, session_id, user_message, facts_json,
                rule_matches_json, response_json, created_at
            ) VALUES (?, ?, '旧咨询', '{}', '[]', '{}', ?)
            """,
            (turn_id, session_id, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO attachments (
                id, status, original_name, media_type, size_bytes,
                sha256, created_at, updated_at, expires_at
            ) VALUES (
                ?, 'processing', '旧附件.pdf', 'application/pdf', 10,
                ?, ?, ?, ?
            )
            """,
            (
                attachment_id,
                "a" * 64,
                NOW.isoformat(),
                NOW.isoformat(),
                (NOW + timedelta(hours=1)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_records (
                id, audit_id, session_id, turn_id, stage, status,
                citations_json, created_at
            ) VALUES (?, ?, ?, ?, 'response', 'ok', '[]', ?)
            """,
            (audit_id, audit_id, session_id, turn_id, NOW.isoformat()),
        )
        connection.execute("PRAGMA user_version = 2")

    store = SessionStore(path, now=lambda: NOW)
    store.initialize()

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]
        owners = {
            table: connection.execute(
                f"SELECT owner_id FROM {table}"
            ).fetchone()[0]
            for table in (
                "sessions",
                "turns",
                "attachments",
                "audit_records",
            )
        }
    assert version == SCHEMA_VERSION == 3
    assert set(owners.values()) == {LOCAL_DEVELOPMENT_OWNER_ID}
    assert store.require_session(session_id).owner_id == (
        LOCAL_DEVELOPMENT_OWNER_ID
    )


def test_postgres_metadata_declares_identity_and_owned_core_tables() -> None:
    expected = {
        "users",
        "consultation_sessions",
        "consultation_turns",
        "consultation_attachments",
        "content_audit_records",
    }
    assert expected.issubset(metadata.tables)
    assert users.c.id.primary_key
    for table in (
        consultation_sessions,
        consultation_turns,
        consultation_attachments,
        content_audit_records,
    ):
        assert table.c.owner_id.nullable is False
        assert any(
            index.columns.keys()[0] == "owner_id"
            for index in table.indexes
        )

    attachment_columns = set(consultation_attachments.c.keys())
    assert "extracted_blocks_json" not in attachment_columns
    assert {
        "confirmed_text",
        "reservation_id",
        "reserved_at",
    }.issubset(attachment_columns)
