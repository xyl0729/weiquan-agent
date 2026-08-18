from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.agent.errors import RequestInputError
from app.agent.pipeline import ConsultationPipeline
from app.api.schemas import ConsultRequest, ConsultResponse
from app.auth.dependencies import (
    get_auth_service,
    require_write_principal,
)
from app.auth.errors import PrivacyRequiredError
from app.auth.principal import (
    LocalPrincipal,
    Principal,
    RegisteredPrincipal,
)
from app.auth.service import AuthService
from app.config import Settings
from app.deps import (
    get_active_settings,
    get_consultation_pipeline,
    get_quota_service,
    request_client_ip,
)
from app.limits.reservations import QuotaCallController, QuotaService
from app.health.system import require_new_work_capacity
from uuid import uuid4


router = APIRouter(prefix="/api", tags=["consultation"])


@router.post("/consult", response_model=ConsultResponse)
async def consult(
    payload: ConsultRequest,
    request: Request,
    principal: Principal = Depends(require_write_principal),
    settings: Settings = Depends(get_active_settings),
    pipeline: ConsultationPipeline = Depends(get_consultation_pipeline),
    auth: AuthService = Depends(get_auth_service),
    quota: QuotaService = Depends(get_quota_service),
) -> ConsultResponse:
    require_new_work_capacity(settings)
    if len(payload.message) > settings.max_message_length:
        raise RequestInputError(
            f"message 长度不能超过 {settings.max_message_length}"
        )
    quota_call = None
    if isinstance(principal, RegisteredPrincipal):
        if auth.requires_privacy_acceptance(
            user_id=principal.user_id,
            context="consultation",
        ):
            raise PrivacyRequiredError()
        quota_call = QuotaCallController(
            quota,
            kind="registered",
            subject_id=principal.user_id,
            logical_call_id=str(uuid4()),
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
        attachment_ids=[
            str(attachment_id)
            for attachment_id in payload.attachment_ids
        ],
        owner_id=principal.user_id,
        quota_call=quota_call,
    )
    public_payload = result.public_payload()
    if not isinstance(principal, LocalPrincipal):
        public_payload["quota"] = quota.registered_status(
            principal.user_id
        ).model_dump(mode="json")
    return ConsultResponse.model_validate(public_payload)
