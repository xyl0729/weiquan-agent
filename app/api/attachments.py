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
from app.api.schemas import AttachmentConfirmRequest
from app.auth.dependencies import (
    require_read_principal,
    require_write_principal,
)
from app.auth.principal import Principal
from app.config import Settings
from app.db.models import AttachmentRecord
from app.deps import (
    get_active_settings,
    get_attachment_service,
)
from app.health.system import require_new_work_capacity


router = APIRouter(prefix="/api/attachments", tags=["attachments"])
_PURGE_LIMIT = 100


@router.post("", response_model=AttachmentReviewPublic)
async def upload_attachment(
    request: Request,
    principal: Principal = Depends(require_write_principal),
    settings: Settings = Depends(get_active_settings),
    service: AttachmentService = Depends(get_attachment_service),
) -> AttachmentReviewPublic:
    require_new_work_capacity(settings)
    service.purge_expired(
        owner_id=principal.user_id,
        limit=_PURGE_LIMIT,
    )
    if not getattr(request.app.state, "ocr_ready", False):
        raise AttachmentServiceUnavailableError()
    record = await service.upload(
        request,
        owner_id=principal.user_id,
    )
    return _public_attachment(record)


@router.get("/{attachment_id}", response_model=AttachmentReviewPublic)
def get_attachment(
    attachment_id: UUID,
    principal: Principal = Depends(require_read_principal),
    service: AttachmentService = Depends(get_attachment_service),
) -> AttachmentReviewPublic:
    service.purge_expired(
        owner_id=principal.user_id,
        limit=_PURGE_LIMIT,
    )
    return _public_attachment(
        service.get(
            str(attachment_id),
            owner_id=principal.user_id,
        )
    )


@router.patch("/{attachment_id}", response_model=AttachmentReviewPublic)
def confirm_attachment(
    attachment_id: UUID,
    payload: AttachmentConfirmRequest,
    principal: Principal = Depends(require_write_principal),
    settings: Settings = Depends(get_active_settings),
    service: AttachmentService = Depends(get_attachment_service),
) -> AttachmentReviewPublic:
    service.purge_expired(
        owner_id=principal.user_id,
        limit=_PURGE_LIMIT,
    )
    if len(payload.confirmed_text) > settings.max_attachment_context_chars:
        raise AttachmentResourceLimitError(
            "attachment_context_too_long"
        )
    record = service.confirm(
        str(attachment_id),
        payload.confirmed_text,
        owner_id=principal.user_id,
    )
    return _public_attachment(record)


@router.delete(
    "/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_attachment(
    attachment_id: UUID,
    principal: Principal = Depends(require_write_principal),
    service: AttachmentService = Depends(get_attachment_service),
) -> Response:
    service.purge_expired(
        owner_id=principal.user_id,
        limit=_PURGE_LIMIT,
    )
    try:
        service.delete(
            str(attachment_id),
            owner_id=principal.user_id,
        )
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
