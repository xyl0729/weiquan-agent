from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.attachments.errors import (
    ATTACHMENT_ERROR_SPECS,
    AttachmentInputError,
    AttachmentNotFoundError,
    AttachmentResourceLimitError,
    AttachmentServiceBusyError,
    AttachmentServiceUnavailableError,
    AttachmentStateConflictError,
    build_attachment_error,
)
from app.attachments.models import (
    AttachmentEvidenceContext,
    AttachmentReviewPublic,
    AttachmentTurnPublic,
    ExtractionBlock,
    ExtractionResult,
)
from app.db.models import AttachmentRecord


def _block(**changes: object) -> ExtractionBlock:
    values: dict[str, object] = {
        "page_number": 1,
        "block_index": 0,
        "text": "订单金额 299 元",
        "confidence": 0.98,
    }
    values.update(changes)
    return ExtractionBlock(**values)


def _result(**changes: object) -> ExtractionResult:
    values: dict[str, object] = {
        "media_type": "application/pdf",
        "page_count": 1,
        "extraction_method": "direct_text",
        "blocks": (_block(),),
        "warnings": (),
    }
    values.update(changes)
    return ExtractionResult(**values)


def _review(**changes: object) -> AttachmentReviewPublic:
    values: dict[str, object] = {
        "id": uuid4(),
        "status": "review_required",
        "original_name": "订单.pdf",
        "media_type": "application/pdf",
        "size_bytes": 1024,
        "page_count": 1,
        "extraction_method": "direct_text",
        "blocks": (_block(),),
        "warnings": (),
        "confirmed_text": None,
        "error_code": None,
    }
    values.update(changes)
    return AttachmentReviewPublic(**values)


def test_attachment_models_forbid_unknown_fields() -> None:
    factories = (
        lambda: ExtractionBlock(
            page_number=1,
            block_index=0,
            text="正文",
            confidence=1,
            internal_path="forbidden",
        ),
        lambda: ExtractionResult(
            media_type="application/pdf",
            page_count=1,
            extraction_method="direct_text",
            blocks=(_block(),),
            warnings=(),
            sha256="forbidden",
        ),
        lambda: AttachmentReviewPublic(
            **_review().model_dump(),
            reservation_id="forbidden",
        ),
        lambda: AttachmentTurnPublic(
            id=uuid4(),
            status="bound",
            original_name="订单.pdf",
            media_type="application/pdf",
            size_bytes=1024,
            page_count=1,
            extraction_method="direct_text",
            warnings=(),
            confirmed_text="订单金额 299 元",
            local_path="forbidden",
        ),
        lambda: AttachmentEvidenceContext(
            id=uuid4(),
            original_name="订单.pdf",
            media_type="application/pdf",
            page_count=1,
            confirmed_text="订单金额 299 元",
            extracted_blocks=(),
        ),
    )

    for factory in factories:
        with pytest.raises(ValidationError):
            factory()


@pytest.mark.parametrize(
    "name",
    ["", "   ", "bad\x00name.pdf", "bad\nname.pdf", "文" * 256],
)
def test_public_attachment_models_reject_invalid_file_names(
    name: str,
) -> None:
    with pytest.raises(ValidationError):
        _review(original_name=name)


def test_extraction_result_checks_page_order_and_image_dimensions() -> None:
    with pytest.raises(ValidationError):
        _result(blocks=(_block(page_number=2),))

    with pytest.raises(ValidationError):
        _result(
            media_type="image/png",
            extraction_method="ocr",
            width_px=None,
            height_px=None,
        )

    image = _result(
        media_type="image/jpeg",
        extraction_method="ocr",
        width_px=1200,
        height_px=800,
    )
    assert image.page_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "processing", "confirmed_text": "不可提前确认"},
        {"status": "review_required", "blocks": ()},
        {"status": "review_required", "confirmed_text": "不可提前确认"},
        {"status": "confirmed", "confirmed_text": "  "},
        {"status": "failed", "error_code": None, "blocks": ()},
        {
            "status": "failed",
            "error_code": "attachment_corrupt",
            "blocks": (_block(),),
        },
        {
            "status": "bound",
            "confirmed_text": None,
        },
        {"status": "unexpected"},
    ],
)
def test_review_projection_rejects_inconsistent_status_data(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _review(**changes)


def test_internal_attachment_can_omit_unconfirmed_ocr_blocks() -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    record = AttachmentRecord(
        id=str(uuid4()),
        owner_id=str(uuid4()),
        status="review_required",
        original_name="订单.pdf",
        media_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
        page_count=1,
        extraction_method="direct_text",
        extracted_blocks=(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
    )

    assert record.extracted_blocks == ()
    with pytest.raises(ValidationError):
        AttachmentReviewPublic(
            id=record.id,
            status=record.status,
            original_name=record.original_name,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            page_count=record.page_count,
            extraction_method=record.extraction_method,
            blocks=record.extracted_blocks,
            warnings=record.warnings,
            confirmed_text=record.confirmed_text,
            error_code=record.error_code,
        )


def test_confirmed_text_is_trimmed_without_truncation() -> None:
    text = "证" * 12_001
    review = _review(status="confirmed", confirmed_text=f"  {text}\n")

    assert review.confirmed_text == text
    assert len(review.confirmed_text) == 12_001


def test_public_projections_and_evidence_are_deeply_immutable() -> None:
    review = _review()
    turn = AttachmentTurnPublic(
        id=uuid4(),
        status="bound",
        original_name="订单.pdf",
        media_type="application/pdf",
        size_bytes=1024,
        page_count=1,
        extraction_method="direct_text",
        warnings=(),
        confirmed_text="订单金额 299 元",
    )
    evidence = AttachmentEvidenceContext(
        id=uuid4(),
        original_name="订单.pdf",
        media_type="application/pdf",
        page_count=1,
        confirmed_text="订单金额 299 元",
    )

    with pytest.raises(ValidationError):
        review.status = "confirmed"
    with pytest.raises(ValidationError):
        turn.confirmed_text = "被修改"
    with pytest.raises(ValidationError):
        evidence.confirmed_text = "被修改"
    assert isinstance(review.blocks, tuple)
    assert isinstance(turn.warnings, tuple)


def test_extraction_result_rejects_duplicate_block_positions_and_warnings() -> None:
    with pytest.raises(ValidationError):
        _result(blocks=(_block(), _block(text="重复位置")))

    with pytest.raises(ValidationError):
        _result(warnings=("low_confidence", "low_confidence"))


def test_attachment_error_catalog_is_complete_and_safe() -> None:
    expected_codes = {
        "attachment_type_unsupported",
        "attachment_type_mismatch",
        "attachment_name_invalid",
        "attachment_pdf_encrypted",
        "attachment_corrupt",
        "attachment_text_empty",
        "attachment_too_large",
        "attachment_page_limit_exceeded",
        "attachment_pixel_limit_exceeded",
        "attachment_extracted_text_too_long",
        "attachment_extraction_timeout",
        "attachment_not_found",
        "attachment_not_reviewable",
        "attachment_not_confirmed",
        "attachment_already_bound",
        "attachment_count_exceeded",
        "attachment_context_too_long",
        "attachment_service_busy",
        "attachment_service_unavailable",
    }

    assert set(ATTACHMENT_ERROR_SPECS) == expected_codes
    for code, spec in ATTACHMENT_ERROR_SPECS.items():
        error = build_attachment_error(code)
        assert error.code == code
        assert error.safe_message == spec.message
        assert error.status_code == spec.status_code
        assert spec.status_code in {404, 409, 413, 422, 503}
        assert spec.message.strip()
        lowered = spec.message.lower()
        assert "rapidocr" not in lowered
        assert "pypdf" not in lowered
        assert "traceback" not in lowered
        assert ":\\" not in spec.message
        assert "/" not in spec.message


def test_attachment_error_factory_uses_narrow_error_types() -> None:
    expected_types = {
        "attachment_type_unsupported": AttachmentInputError,
        "attachment_too_large": AttachmentResourceLimitError,
        "attachment_not_found": AttachmentNotFoundError,
        "attachment_not_reviewable": AttachmentStateConflictError,
        "attachment_service_busy": AttachmentServiceBusyError,
        "attachment_service_unavailable": (
            AttachmentServiceUnavailableError
        ),
    }

    for code, expected_type in expected_types.items():
        assert isinstance(build_attachment_error(code), expected_type)
