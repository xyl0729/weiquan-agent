from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.attachments.errors import (
    AttachmentNotFoundError,
    AttachmentStateConflictError,
)
from app.attachments.models import ExtractionBlock, ExtractionResult
from app.attachments.store import AttachmentStore
from app.db.contracts import (
    AttachmentBindingCommand,
    ConsultationCommitCommand,
    SessionUpdateCommand,
    TurnWriteCommand,
)
from app.db.session import SessionStore


NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def _stores(
    path: Path,
    *,
    session_ttl_hours: int = 72,
) -> tuple[SessionStore, AttachmentStore]:
    sessions = SessionStore(
        path,
        ttl_hours=session_ttl_hours,
        now=lambda: NOW,
    )
    sessions.initialize()
    attachments = AttachmentStore(
        sessions,
        draft_ttl_seconds=3600,
        now=lambda: NOW,
    )
    return sessions, attachments


def _result(text: str = "订单金额 299 元") -> ExtractionResult:
    return ExtractionResult(
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
    )


def _processing(
    store: AttachmentStore,
    *,
    name: str = "订单.pdf",
    now: datetime | None = None,
):
    return store.create_processing(
        original_name=name,
        media_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
        now=now,
    )


def _confirmed(store: AttachmentStore, *, text: str = "订单金额 299 元"):
    record = _processing(store)
    store.save_extraction(record.id, _result(text))
    return store.confirm(record.id, f"  {text}\n")


def _turn(sessions: SessionStore, *, now: datetime | None = None):
    session = sessions.create_session(now=now)
    turn = sessions.add_turn(
        session.id,
        user_message="请帮我看这份订单",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
        now=now,
    )
    return session, turn


def test_extraction_confirmation_and_edit_are_atomic(
    tmp_path: Path,
) -> None:
    _, attachments = _stores(tmp_path / "app.db")
    processing = _processing(attachments)

    review = attachments.save_extraction(processing.id, _result())
    confirmed = attachments.confirm(
        processing.id,
        "  订单金额 299 元\n",
    )
    edited = attachments.confirm(
        processing.id,
        "订单金额 399 元",
        now=NOW + timedelta(minutes=1),
    )

    assert review.status == "review_required"
    assert review.confirmed_text is None
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_text == "订单金额 299 元"
    assert edited.confirmed_text == "订单金额 399 元"
    assert edited.updated_at == NOW + timedelta(minutes=1)


def test_failed_attachment_cannot_be_confirmed(
    tmp_path: Path,
) -> None:
    _, attachments = _stores(tmp_path / "app.db")
    processing = _processing(attachments)
    failed = attachments.save_failure(
        processing.id,
        "attachment_corrupt",
    )

    assert failed.status == "failed"
    with pytest.raises(AttachmentStateConflictError) as caught:
        attachments.confirm(processing.id, "不可确认")
    assert caught.value.code == "attachment_not_reviewable"


def test_reservation_is_ordered_exclusive_and_releasable(
    tmp_path: Path,
) -> None:
    _, attachments = _stores(tmp_path / "app.db")
    first = _confirmed(attachments, text="第一份")
    second = _confirmed(attachments, text="第二份")
    reservation_id = str(uuid4())

    reserved = attachments.reserve(
        [second.id, first.id],
        reservation_id=reservation_id,
    )

    assert reserved == reservation_id
    assert attachments.get(second.id).turn_position == 0
    assert attachments.get(first.id).turn_position == 1
    with pytest.raises(AttachmentStateConflictError):
        attachments.reserve([first.id])

    assert attachments.release(reservation_id) == 2
    assert attachments.get(first.id).status == "confirmed"
    assert attachments.get(first.id).reservation_id is None
    assert attachments.get(first.id).turn_position is None
    assert attachments.release(reservation_id) == 0


def test_concurrent_reservation_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    _, attachments = _stores(tmp_path / "app.db")
    record = _confirmed(attachments)

    def reserve(reservation_id: str) -> str:
        try:
            attachments.reserve(
                [record.id],
                reservation_id=reservation_id,
            )
        except AttachmentStateConflictError:
            return "conflict"
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                reserve,
                [str(uuid4()), str(uuid4())],
            )
        )

    assert sorted(outcomes) == ["conflict", "reserved"]


def test_bind_requires_matching_reservation_and_preserves_order(
    tmp_path: Path,
) -> None:
    sessions, attachments = _stores(tmp_path / "app.db")
    first = _confirmed(attachments, text="第一份")
    second = _confirmed(attachments, text="第二份")
    session, turn = _turn(sessions)
    reservation_id = attachments.reserve([second.id, first.id])

    with pytest.raises(AttachmentStateConflictError):
        attachments.bind_reserved(
            str(uuid4()),
            session_id=session.id,
            turn_id=turn.id,
            expected_ids=[second.id, first.id],
        )

    bound = attachments.bind_reserved(
        reservation_id,
        session_id=session.id,
        turn_id=turn.id,
        expected_ids=[second.id, first.id],
    )

    assert [item.id for item in bound] == [second.id, first.id]
    assert all(item.status == "bound" for item in bound)
    assert all(item.reservation_id is None for item in bound)
    assert [
        item.id for item in attachments.list_for_turn(turn.id)
    ] == [second.id, first.id]
    with pytest.raises(AttachmentStateConflictError):
        attachments.delete(first.id)


def test_turn_rejects_a_second_attachment_binding(
    tmp_path: Path,
) -> None:
    sessions, attachments = _stores(tmp_path / "app.db")
    first = _confirmed(attachments, text="第一批")
    second = _confirmed(attachments, text="第二批")
    session, turn = _turn(sessions)
    first_reservation = attachments.reserve([first.id])
    attachments.bind_reserved(
        first_reservation,
        session_id=session.id,
        turn_id=turn.id,
        expected_ids=[first.id],
    )
    second_reservation = attachments.reserve([second.id])

    with pytest.raises(AttachmentStateConflictError):
        attachments.bind_reserved(
            second_reservation,
            session_id=session.id,
            turn_id=turn.id,
            expected_ids=[second.id],
        )

    restored = attachments.get(second.id)
    assert restored.status == "confirmed"
    assert restored.reservation_id == second_reservation


def test_cross_session_bind_rolls_back_without_moving_attachment(
    tmp_path: Path,
) -> None:
    sessions, attachments = _stores(tmp_path / "app.db")
    record = _confirmed(attachments)
    reservation_id = attachments.reserve([record.id])
    first_session, first_turn = _turn(sessions)
    second_session = sessions.create_session()

    with pytest.raises(sqlite3.IntegrityError):
        attachments.bind_reserved(
            reservation_id,
            session_id=second_session.id,
            turn_id=first_turn.id,
            expected_ids=[record.id],
        )

    restored = attachments.get(record.id)
    assert restored.status == "confirmed"
    assert restored.session_id is None
    assert restored.turn_id is None
    assert restored.reservation_id == reservation_id
    assert first_session.id != second_session.id


def test_session_delete_and_expiry_cascade_bound_attachments(
    tmp_path: Path,
) -> None:
    sessions, attachments = _stores(
        tmp_path / "app.db",
        session_ttl_hours=1,
    )
    first = _confirmed(attachments)
    first_session, first_turn = _turn(sessions)
    first_reservation = attachments.reserve([first.id])
    attachments.bind_reserved(
        first_reservation,
        session_id=first_session.id,
        turn_id=first_turn.id,
        expected_ids=[first.id],
    )

    assert sessions.get_session(
        first_session.id,
        now=NOW + timedelta(hours=1),
    ) is None
    assert attachments.get_optional(first.id) is None


def test_expired_draft_cleanup_is_bounded_and_keeps_fresh_drafts(
    tmp_path: Path,
) -> None:
    _, attachments = _stores(tmp_path / "app.db")
    expired = [
        _processing(
            attachments,
            name=f"过期-{index}.pdf",
            now=NOW,
        )
        for index in range(3)
    ]
    fresh = _processing(
        attachments,
        name="仍有效.pdf",
        now=NOW + timedelta(minutes=30),
    )

    assert attachments.purge_expired(
        now=NOW + timedelta(hours=1),
        limit=2,
    ) == 2
    remaining_expired = [
        item
        for item in expired
        if attachments.get_optional(item.id) is not None
    ]
    assert len(remaining_expired) == 1
    assert attachments.get_optional(fresh.id) is not None


def test_expired_draft_is_unavailable_before_physical_cleanup(
    tmp_path: Path,
) -> None:
    _, attachments = _stores(tmp_path / "app.db")
    record = _processing(attachments)
    expiry = NOW + timedelta(hours=1)

    assert attachments.get_optional(record.id, now=expiry) is None
    with pytest.raises(AttachmentNotFoundError):
        attachments.get(record.id, now=expiry)

    assert attachments.get_optional(record.id, now=NOW) is not None


def test_atomic_session_turn_and_binding_roll_back_together(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    sessions, attachments = _stores(path)
    session = sessions.create_session()
    record = _confirmed(attachments)
    reservation_id = attachments.reserve([record.id])
    turn_id = str(uuid4())
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_attachment_binding
            BEFORE UPDATE OF status ON attachments
            WHEN NEW.status = 'bound'
            BEGIN
                SELECT RAISE(ABORT, 'injected persistence failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        sessions.persist_session_turn(
            ConsultationCommitCommand(
                owner_id=session.owner_id,
                session_id=session.id,
                session=SessionUpdateCommand(
                    scenario_id="deposit_deduction",
                    facts={"deposit_amount": 2000},
                    followup_round=1,
                    status="need_more_facts",
                    jurisdiction="CN",
                ),
                turn=TurnWriteCommand(
                    turn_id=turn_id,
                    user_message="房东扣了押金",
                    facts={"deposit_amount": 2000},
                    rule_matches=(),
                    response={"status": "need_more_facts"},
                ),
                attachment_binding=AttachmentBindingCommand(
                    reservation_id=reservation_id,
                    attachment_ids=(record.id,),
                ),
                occurred_at=NOW,
            )
        )

    restored_session = sessions.require_session(session.id)
    restored_attachment = attachments.get(record.id)
    assert restored_session.scenario_id is None
    assert restored_session.facts == {}
    assert restored_session.followup_round == 0
    assert sessions.list_turns(session.id) == []
    assert restored_attachment.status == "confirmed"
    assert restored_attachment.reservation_id == reservation_id
    assert restored_attachment.turn_id is None
