from __future__ import annotations

from pydantic import ValidationError

from app.agent.errors import DataIntegrityError
from app.attachments.models import AttachmentTurnPublic
from app.db.models import AttachmentRecord


def attachment_turn_public(
    record: AttachmentRecord,
) -> AttachmentTurnPublic:
    if (
        record.status != "bound"
        or record.page_count is None
        or record.extraction_method is None
        or record.confirmed_text is None
    ):
        raise _projection_error()
    try:
        return AttachmentTurnPublic(
            id=record.id,
            original_name=record.original_name,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            page_count=record.page_count,
            extraction_method=record.extraction_method,
            warnings=record.warnings,
            confirmed_text=record.confirmed_text,
        )
    except (ValidationError, ValueError) as exc:
        raise _projection_error() from exc


def _projection_error() -> DataIntegrityError:
    return DataIntegrityError(
        "attachment_projection_invalid",
        "附件公开信息完整性检查失败",
    )
