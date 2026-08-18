from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.schemas import (
    AuthAcceptedResponse,
    AuthCsrfResponse,
    AuthUserResponse,
    CaptchaConfigResponse,
    ForgotPasswordRequest,
    LoginRequest,
    RegisteredQuotaResponse,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    UserResponse,
    VerifyEmailRequest,
)
from app.auth.dependencies import (
    get_auth_service,
    require_auth_context,
    require_csrf_context,
    require_same_origin,
)
from app.auth.models import AuthContext, UserRecord
from app.auth.service import AuthService
from app.config import Settings
from app.deps import (
    get_active_settings,
    get_quota_service,
    request_client_ip,
)
from app.limits.reservations import QuotaService


router = APIRouter(prefix="/api/auth", tags=["authentication"])
same_origin = [Depends(require_same_origin)]


@router.get(
    "/captcha-config",
    response_model=CaptchaConfigResponse,
)
def captcha_config(
    settings: Settings = Depends(get_active_settings),
) -> CaptchaConfigResponse:
    enabled = (
        settings.deployment_mode == "production"
        and settings.captcha_enabled
    )
    return CaptchaConfigResponse(
        enabled=enabled,
        scene_id=settings.captcha_scene_id if enabled else "",
        prefix=settings.captcha_prefix if enabled else "",
    )


@router.post(
    "/register",
    response_model=AuthAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=same_origin,
)
def register(
    payload: RegisterRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AuthAcceptedResponse:
    service.register(
        email=payload.email,
        password=payload.password.get_secret_value(),
        captcha_token=(
            payload.captcha_token.get_secret_value()
            if payload.captcha_token is not None
            else ""
        ),
        privacy_version=payload.privacy_version,
        privacy_accepted=payload.privacy_accepted,
        client_key=request_client_ip(request),
    )
    return AuthAcceptedResponse(status="accepted")


@router.post(
    "/resend-verification",
    response_model=AuthAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=same_origin,
)
def resend_verification(
    payload: ResendVerificationRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AuthAcceptedResponse:
    service.resend_verification(
        email=payload.email,
        client_key=request_client_ip(request),
    )
    return AuthAcceptedResponse(status="accepted")


@router.post(
    "/verify",
    response_model=AuthUserResponse,
    dependencies=same_origin,
)
def verify_email(
    payload: VerifyEmailRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
    quota: QuotaService = Depends(get_quota_service),
) -> AuthUserResponse:
    user = service.verify_email(
        email=payload.email,
        code=payload.code.get_secret_value(),
        client_key=request_client_ip(request),
    )
    return _auth_user_response(user, service=service, quota=quota)


@router.post(
    "/login",
    response_model=AuthUserResponse,
    dependencies=same_origin,
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_active_settings),
    service: AuthService = Depends(get_auth_service),
    quota: QuotaService = Depends(get_quota_service),
) -> AuthUserResponse:
    result = service.login(
        email=payload.email,
        password=payload.password.get_secret_value(),
        client_key=request_client_ip(request),
    )
    response.set_cookie(
        key=settings.cookie_name,
        value=result.session_token,
        max_age=settings.auth_session_ttl_seconds,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return _auth_user_response(
        result.user,
        service=service,
        quota=quota,
    )


@router.get("/me", response_model=AuthUserResponse)
def me(
    context: AuthContext = Depends(require_auth_context),
    service: AuthService = Depends(get_auth_service),
    quota: QuotaService = Depends(get_quota_service),
) -> AuthUserResponse:
    return _auth_user_response(
        context.user,
        service=service,
        quota=quota,
    )


@router.get("/csrf", response_model=AuthCsrfResponse)
def csrf(
    request: Request,
    context: AuthContext = Depends(require_auth_context),
    service: AuthService = Depends(get_auth_service),
) -> AuthCsrfResponse:
    del context
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.cookie_name, "")
    return AuthCsrfResponse(csrf_token=service.issue_csrf(token))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    settings: Settings = Depends(get_active_settings),
    context: AuthContext = Depends(require_csrf_context),
    service: AuthService = Depends(get_auth_service),
) -> Response:
    del context
    token = request.cookies.get(settings.cookie_name, "")
    service.logout(token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=settings.cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post(
    "/forgot-password",
    response_model=AuthAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=same_origin,
)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AuthAcceptedResponse:
    service.forgot_password(
        email=payload.email,
        client_key=request_client_ip(request),
    )
    return AuthAcceptedResponse(status="accepted")


@router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=same_origin,
)
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> Response:
    service.reset_password(
        token=payload.token.get_secret_value(),
        new_password=payload.new_password.get_secret_value(),
        client_key=request_client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _auth_user_response(
    user: UserRecord,
    *,
    service: AuthService,
    quota: QuotaService,
) -> AuthUserResponse:
    quota_status = quota.registered_status(user.id)
    return AuthUserResponse(
        user=UserResponse(
            id=user.id,
            email=user.email,
            role=user.role,
            status=user.status,
            verified_at=user.verified_at,
            created_at=user.created_at,
        ),
        quota=RegisteredQuotaResponse.model_validate(
            quota_status.model_dump()
        ),
        privacy_version=service.policy.version,
        privacy_acceptance_required=(
            service.requires_privacy_acceptance(
                user_id=user.id,
                context="consultation",
            )
        ),
    )
