from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.agent.errors import RequestInputError
from app.agent.pipeline import ConsultationPipeline
from app.api.schemas import ConsultRequest, ConsultResponse
from app.config import Settings
from app.deps import (
    get_active_settings,
    get_consultation_pipeline,
    request_client_ip,
)


router = APIRouter(prefix="/api", tags=["consultation"])


@router.post("/consult", response_model=ConsultResponse)
async def consult(
    payload: ConsultRequest,
    request: Request,
    settings: Settings = Depends(get_active_settings),
    pipeline: ConsultationPipeline = Depends(get_consultation_pipeline),
) -> ConsultResponse:
    if len(payload.message) > settings.max_message_length:
        raise RequestInputError(
            f"message 长度不能超过 {settings.max_message_length}"
        )
    result = await pipeline.consult(
        message=payload.message,
        session_id=(
            str(payload.session_id)
            if payload.session_id is not None
            else None
        ),
        jurisdiction=payload.jurisdiction,
        client_identifier=request_client_ip(request),
    )
    return ConsultResponse.model_validate(result.public_payload())
