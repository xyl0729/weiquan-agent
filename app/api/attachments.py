from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from app.attachments.errors import (
    AttachmentNotFoundError,
    AttachmentResourceLimitError,
    AttachmentServiceUnavailableError,
)
from app.attachments.models import AttachmentReviewPublic
from app.attachments.service import AttachmentService
from app.attachments.store import AttachmentStore
from app.api.schemas import AttachmentConfirmRequest
from app.config import Settings
from app.db.models import AttachmentRecord
from app.deps import (
    get_active_settings,
    get_attachment_service,
    get_attachment_store,
)


router = APIRouter(prefix="/api/attachments", tags=["attachments"])
_PURGE_LIMIT = 100


@router.post("", response_model=AttachmentReviewPublic)
async def upload_attachment(
    request: Request,
    service: AttachmentService = Depends(get_attachment_service),
) -> AttachmentReviewPublic:
    service.store.purge_expired(limit=_PURGE_LIMIT)
    if not getattr(request.app.state, "ocr_ready", False):
        raise AttachmentServiceUnavailableError()
    record = await service.upload(request)
    return _public_attachment(record)


@router.get("/{attachment_id}", response_model=AttachmentReviewPublic)
def get_attachment(
    attachment_id: UUID,
    store: AttachmentStore = Depends(get_attachment_store),
) -> AttachmentReviewPublic:
    store.purge_expired(limit=_PURGE_LIMIT)
    return _public_attachment(store.get(str(attachment_id)))


@router.patch("/{attachment_id}", response_model=AttachmentReviewPublic)
def confirm_attachment(
    attachment_id: UUID,
    payload: AttachmentConfirmRequest,
    settings: Settings = Depends(get_active_settings),
    store: AttachmentStore = Depends(get_attachment_store),
) -> AttachmentReviewPublic:
    store.purge_expired(limit=_PURGE_LIMIT)
    if len(payload.confirmed_text) > settings.max_attachment_context_chars:
        raise AttachmentResourceLimitError(
            "attachment_context_too_long"
        )
    record = store.confirm(
        str(attachment_id),
        payload.confirmed_text,
    )
    return _public_attachment(record)


@router.delete(
    "/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_attachment(
    attachment_id: UUID,
    store: AttachmentStore = Depends(get_attachment_store),
) -> Response:
    store.purge_expired(limit=_PURGE_LIMIT)
    try:
        store.delete(str(attachment_id))
    except AttachmentNotFoundError:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _public_attachment(
    record: AttachmentRecord,
) -> AttachmentReviewPublic:
    return AttachmentReviewPublic(
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
