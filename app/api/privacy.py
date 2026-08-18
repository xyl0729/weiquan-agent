from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.schemas import (
    PrivacyAcceptRequest,
    PrivacyPolicyResponse,
)
from app.auth.dependencies import (
    get_auth_service,
    require_csrf_context,
)
from app.auth.models import AuthContext
from app.auth.service import AuthService


router = APIRouter(prefix="/api/privacy", tags=["privacy"])


@router.get("", response_model=PrivacyPolicyResponse)
def privacy_policy(
    service: AuthService = Depends(get_auth_service),
) -> PrivacyPolicyResponse:
    return PrivacyPolicyResponse(
        version=service.policy.version,
        text=service.policy.text,
    )


@router.post("/accept", response_model=PrivacyPolicyResponse)
def accept_privacy(
    payload: PrivacyAcceptRequest,
    context: AuthContext = Depends(require_csrf_context),
    service: AuthService = Depends(get_auth_service),
) -> PrivacyPolicyResponse:
    service.accept_privacy(
        user_id=context.user.id,
        context=payload.context,
        policy_version=payload.policy_version,
    )
    return PrivacyPolicyResponse(
        version=service.policy.version,
        text=service.policy.text,
    )
