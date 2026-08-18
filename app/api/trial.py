from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response

from app.agent.pipeline import ConsultationPipeline
from app.api.schemas import (
    ConsultResponse,
    TrialConsultRequest,
    TrialQuotaResponse,
    TrialStartRequest,
    TrialStartResponse,
)
from app.auth.dependencies import require_same_origin
from app.config import Settings
from app.deps import (
    get_active_settings,
    get_consultation_pipeline,
    get_quota_service,
    get_trial_conversation_store,
    get_trial_identity_manager,
    request_client_ip,
)
from app.trial.conversations import InMemoryTrialConversationStore
from app.limits.reservations import QuotaCallController, QuotaService
from app.health.system import require_new_work_capacity
from app.observability.request_context import set_request_identity
from app.trial.identity import TrialIdentityManager
from app.trial.models import TrialIdentityRequiredError


router = APIRouter(
    prefix="/api/trial",
    tags=["trial"],
    dependencies=[Depends(require_same_origin)],
)


@router.post("/start", response_model=TrialStartResponse)
def start_trial(
    payload: TrialStartRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_active_settings),
    manager: TrialIdentityManager = Depends(
        get_trial_identity_manager
    ),
    quota: QuotaService = Depends(get_quota_service),
) -> TrialStartResponse:
    started = manager.start(
        existing_token=request.cookies.get(
            settings.trial_cookie_name
        ),
        captcha_token=(
            payload.captcha_token.get_secret_value()
            if payload.captcha_token is not None
            else ""
        ),
        privacy_version=payload.privacy_version,
        privacy_accepted=payload.privacy_accepted,
        client_ip=request_client_ip(request),
    )
    if started.cookie_value is not None:
        response.set_cookie(
            key=settings.trial_cookie_name,
            value=started.cookie_value,
            max_age=settings.trial_cookie_ttl_seconds,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
    set_request_identity("trial", started.identity.id)
    return TrialStartResponse(
        identity_id=started.identity.id,
        quota=TrialQuotaResponse.model_validate(
            quota.trial_status(started.identity.id).model_dump()
        ),
    )


@router.post("/consult", response_model=ConsultResponse)
async def trial_consult(
    payload: TrialConsultRequest,
    request: Request,
    settings: Settings = Depends(get_active_settings),
    manager: TrialIdentityManager = Depends(
        get_trial_identity_manager
    ),
    quota: QuotaService = Depends(get_quota_service),
    pipeline: ConsultationPipeline = Depends(get_consultation_pipeline),
    conversations: InMemoryTrialConversationStore = Depends(
        get_trial_conversation_store
    ),
) -> ConsultResponse:
    require_new_work_capacity(settings)
    identity = manager.authenticate(
        request.cookies.get(settings.trial_cookie_name)
    )
    if identity is None:
        raise TrialIdentityRequiredError()
    set_request_identity("trial", identity.id)
    if len(payload.message) > settings.trial_max_message_length:
        # The public schema enforces the fixed 3,000-character ceiling;
        # this preserves a stricter configured ceiling if one is used.
        from app.agent.errors import RequestInputError

        raise RequestInputError(
            "message 长度不能超过 "
            f"{settings.trial_max_message_length}"
        )

    manager.activate_for_consult(identity)
    quota_call = QuotaCallController(
        quota,
        kind="trial",
        subject_id=identity.id,
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
        owner_id=identity.id,
        persist=False,
        transient_store=conversations,
        quota_call=quota_call,
    )
    public_payload = result.public_payload()
    public_payload["quota"] = quota.trial_status(
        identity.id
    ).model_dump(mode="json")
    return ConsultResponse.model_validate(public_payload)
