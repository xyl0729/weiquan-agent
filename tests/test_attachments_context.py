from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.attachments.context import EvidenceContextBuilder
from app.attachments.errors import (
    AttachmentNotFoundError,
    AttachmentResourceLimitError,
    AttachmentStateConflictError,
)
from app.attachments.models import ExtractionBlock, ExtractionResult
from app.attachments.store import AttachmentStore
from app.db.contracts import LOCAL_DEVELOPMENT_OWNER_ID
from app.db.session import SessionStore


NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def _stores(path: Path) -> tuple[SessionStore, AttachmentStore]:
    sessions = SessionStore(path, now=lambda: NOW)
    sessions.initialize()
    attachments = AttachmentStore(
        sessions,
        draft_ttl_seconds=3600,
        now=lambda: NOW,
    )
    return sessions, attachments


def _confirmed(
    store: AttachmentStore,
    *,
    name: str = "订单.pdf",
    text: str = "订单金额 299 元",
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
                    confidence=0.61,
                ),
            ),
        ),
    )
    return store.confirm(processing.id, text)


def test_builder_preserves_request_order_and_exposes_only_confirmed_fields(
    tmp_path: Path,
) -> None:
    _, store = _stores(tmp_path / "app.db")
    first = _confirmed(
        store,
        name="合同.pdf",
        text="合同金额 299 元",
    )
    second = _confirmed(
        store,
        name="聊天记录.pdf",
        text="商家明确拒绝退款",
    )
    reservation_id = store.reserve([second.id, first.id])

    evidence = EvidenceContextBuilder(store).build(
        [second.id, first.id],
        owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
        reservation_id=reservation_id,
    )

    assert isinstance(evidence, tuple)
    assert [str(item.id) for item in evidence] == [second.id, first.id]
    assert [item.confirmed_text for item in evidence] == [
        "商家明确拒绝退款",
        "合同金额 299 元",
    ]
    assert all(
        set(item.model_dump()) == {
            "id",
            "original_name",
            "media_type",
            "page_count",
            "confirmed_text",
        }
        for item in evidence
    )
    serialized = repr(
        [item.model_dump(mode="json") for item in evidence]
    )
    assert "extracted_blocks" not in serialized
    assert "confidence" not in serialized
    assert "sha256" not in serialized
    assert "reservation_id" not in serialized


def test_builder_accepts_exact_character_limit_and_never_truncates(
    tmp_path: Path,
) -> None:
    _, store = _stores(tmp_path / "app.db")
    first = _confirmed(store, text="甲" * 6000)
    second = _confirmed(store, text="乙" * 6000)
    reservation_id = store.reserve([first.id, second.id])
    builder = EvidenceContextBuilder(store, max_context_chars=12_000)

    evidence = builder.build(
        [first.id, second.id],
        owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
        reservation_id=reservation_id,
    )

    assert sum(len(item.confirmed_text) for item in evidence) == 12_000
    assert evidence[0].confirmed_text == "甲" * 6000
    assert evidence[1].confirmed_text == "乙" * 6000

    assert store.release(reservation_id) == 2
    third = _confirmed(store, text="丙")
    oversized_reservation = store.reserve([first.id, second.id, third.id])

    with pytest.raises(AttachmentResourceLimitError) as exc_info:
        builder.build(
            [first.id, second.id, third.id],
            owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
            reservation_id=oversized_reservation,
        )

    assert exc_info.value.code == "attachment_context_too_long"


def test_builder_rejects_duplicate_and_excess_attachment_ids(
    tmp_path: Path,
) -> None:
    _, store = _stores(tmp_path / "app.db")
    records = [_confirmed(store, text=str(index)) for index in range(4)]
    reservation_id = store.reserve([records[0].id])
    builder = EvidenceContextBuilder(store)

    with pytest.raises(ValueError, match="重复"):
        builder.build(
            [records[0].id, records[0].id],
            owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
            reservation_id=reservation_id,
        )

    with pytest.raises(AttachmentResourceLimitError) as exc_info:
        builder.build(
            [record.id for record in records],
            owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
            reservation_id=reservation_id,
        )

    assert exc_info.value.code == "attachment_count_exceeded"


def test_builder_rejects_missing_unconfirmed_and_failed_attachments(
    tmp_path: Path,
) -> None:
    _, store = _stores(tmp_path / "app.db")
    builder = EvidenceContextBuilder(store)
    reservation_id = str(uuid4())

    with pytest.raises(AttachmentNotFoundError):
        builder.build(
            [str(uuid4())],
            owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
            reservation_id=reservation_id,
        )

    review = store.create_processing(
        original_name="待核对.pdf",
        media_type="application/pdf",
        size_bytes=10,
        sha256="b" * 64,
    )
    store.save_extraction(
        review.id,
        ExtractionResult(
            media_type="application/pdf",
            page_count=1,
            extraction_method="direct_text",
            blocks=(
                ExtractionBlock(
                    page_number=1,
                    block_index=0,
                    text="待核对文字",
                    confidence=1,
                ),
            ),
        ),
    )
    failed = store.create_processing(
        original_name="损坏.pdf",
        media_type="application/pdf",
        size_bytes=10,
        sha256="c" * 64,
    )
    store.save_failure(failed.id, "attachment_corrupt")

    for attachment_id in (review.id, failed.id):
        with pytest.raises(AttachmentStateConflictError) as exc_info:
            builder.build(
                [attachment_id],
                owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
                reservation_id=reservation_id,
            )
        assert exc_info.value.code == "attachment_not_confirmed"


def test_builder_requires_the_current_reservation_and_rejects_bound_records(
    tmp_path: Path,
) -> None:
    sessions, store = _stores(tmp_path / "app.db")
    record = _confirmed(store)
    reservation_id = store.reserve([record.id])
    builder = EvidenceContextBuilder(store)

    with pytest.raises(AttachmentStateConflictError) as exc_info:
        builder.build(
            [record.id],
            owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
            reservation_id=str(uuid4()),
        )
    assert exc_info.value.code == "attachment_already_bound"

    session = sessions.create_session()
    turn = sessions.add_turn(
        session.id,
        user_message="请帮我看订单",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
    )
    store.bind_reserved(
        reservation_id,
        session_id=session.id,
        turn_id=turn.id,
        expected_ids=[record.id],
    )

    with pytest.raises(AttachmentStateConflictError) as exc_info:
        builder.build(
            [record.id],
            owner_id=LOCAL_DEVELOPMENT_OWNER_ID,
            reservation_id=reservation_id,
        )
    assert exc_info.value.code == "attachment_already_bound"
