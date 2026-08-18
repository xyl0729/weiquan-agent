from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request

from app.auth.errors import (
    AuthenticationFailedError,
    RegistrationRequiredError,
    SameOriginRequiredError,
)
from app.auth.models import AuthContext
from app.auth.passwords import PasswordManager
from app.auth.principal import (
    LocalPrincipal,
    Principal,
    RegisteredPrincipal,
)
from app.auth.service import AuthService
from app.auth.store import InMemoryAuthStore, PostgresAuthStore
from app.config import Settings
from app.db.engine import create_database_engine
from app.integrations.captcha import (
    AliyunCaptchaVerifier,
    DisabledCaptchaVerifier,
)
from app.integrations.directmail import (
    AliyunDirectMailSender,
    InMemoryMailSender,
)
from app.observability.request_context import set_request_identity
from app.observability.metrics import OperationalMetrics
from app.privacy.policy import load_privacy_policy


def initialize_auth_dependencies(application: FastAPI) -> AuthService:
    existing = getattr(application.state, "auth_service", None)
    if isinstance(existing, AuthService):
        return existing

    settings: Settings = application.state.settings
    if settings.deployment_mode == "production":
        operational_metrics = getattr(
            application.state,
            "operational_metrics",
            None,
        )
        if not isinstance(operational_metrics, OperationalMetrics):
            operational_metrics = OperationalMetrics()
            application.state.operational_metrics = operational_metrics
        engine = getattr(application.state, "database_engine", None)
        if engine is None:
            engine = create_database_engine(settings)
            application.state.database_engine = engine
        store = PostgresAuthStore(engine)
        access_key_id = _secret_value(
            settings.aliyun_access_key_id,
            name="ALIYUN_ACCESS_KEY_ID",
        )
        access_key_secret = _secret_value(
            settings.aliyun_access_key_secret,
            name="ALIYUN_ACCESS_KEY_SECRET",
        )
        mailer = AliyunDirectMailSender.from_credentials(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            account_name=settings.directmail_account_name,
            from_alias=settings.directmail_from_alias,
            region=settings.directmail_region,
            outcome_recorder=lambda outcome: operational_metrics.record(
                "mail",
                outcome,
            ),
        )
        if settings.captcha_enabled:
            captcha = AliyunCaptchaVerifier.from_credentials(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                scene_id=settings.captcha_scene_id,
                endpoint=settings.captcha_endpoint,
                outcome_recorder=lambda outcome: (
                    operational_metrics.record("captcha", outcome)
                ),
            )
        else:
            captcha = DisabledCaptchaVerifier()
        ip_secret = _secret_value(
            settings.ip_hmac_secret,
            name="IP_HMAC_SECRET",
        ).encode("utf-8")
        rate_limit_secret = hmac.new(
            ip_secret,
            b"weiquan-auth-rate-limit-v1",
            hashlib.sha256,
        ).digest()
        verification_code_secret = hmac.new(
            ip_secret,
            b"weiquan-email-verification-code-v1",
            hashlib.sha256,
        ).digest()
    else:
        store = InMemoryAuthStore(
            capacity_limit=settings.registration_capacity_limit
        )
        mailer = InMemoryMailSender()
        captcha = DisabledCaptchaVerifier()
        rate_limit_secret = hashlib.sha256(
            b"weiquan-local-auth-rate-limit-v1"
        ).digest()
        verification_code_secret = hashlib.sha256(
            b"weiquan-local-email-verification-code-v1"
        ).digest()

    public_base_url = settings.public_base_url
    if public_base_url is None:
        public_base_url = (
            settings.allowed_origins[0]
            if settings.allowed_origins
            else "http://localhost:8000"
        )
    service = AuthService(
        store=store,
        passwords=PasswordManager(),
        mailer=mailer,
        captcha=captcha,
        policy=load_privacy_policy(
            version=settings.privacy_policy_version
        ),
        public_base_url=public_base_url,
        rate_limit_secret=rate_limit_secret,
        verification_code_secret=verification_code_secret,
        session_ttl=timedelta(
            seconds=settings.auth_session_ttl_seconds
        ),
        token_ttl=timedelta(
            minutes=settings.auth_token_ttl_minutes
        ),
        pending_ttl=timedelta(
            hours=settings.pending_registration_ttl_hours
        ),
        rate_limit_window=timedelta(
            seconds=settings.auth_rate_limit_window_seconds
        ),
        rate_limits=settings.auth_rate_limits,
        rollout_stage=settings.rollout_stage,
        invited_user_limit=settings.invited_user_limit,
    )
    application.state.auth_store = store
    application.state.auth_mailer = mailer
    application.state.auth_captcha = captcha
    application.state.auth_service = service
    return service


def get_auth_service(request: Request) -> AuthService:
    return initialize_auth_dependencies(request.app)


def require_same_origin(
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> None:
    del service
    settings: Settings = request.app.state.settings
    expected = settings.public_origin or _request_origin(request)
    supplied = request.headers.get("origin")
    if supplied is None:
        supplied = _origin_from_referer(
            request.headers.get("referer")
        )
    else:
        supplied = _normalize_origin(supplied)
    if (
        expected is None
        or supplied is None
        or not hmac.compare_digest(expected, supplied)
    ):
        raise SameOriginRequiredError()


def require_auth_context(
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AuthContext:
    context = service.authenticate(_session_token(request))
    set_request_identity("user", context.user.id)
    return context


def require_csrf_context(
    request: Request,
    _same_origin: None = Depends(require_same_origin),
    service: AuthService = Depends(get_auth_service),
) -> AuthContext:
    del _same_origin
    context = service.validate_csrf(
        _session_token(request),
        request.headers.get("x-csrf-token", ""),
    )
    set_request_identity("user", context.user.id)
    return context


def require_read_principal(
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> Principal:
    settings: Settings = request.app.state.settings
    if settings.deployment_mode == "local":
        principal = LocalPrincipal()
        set_request_identity("local", principal.user_id)
        return principal
    return _registered_principal(request, service)


def require_write_principal(
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> Principal:
    settings: Settings = request.app.state.settings
    if settings.deployment_mode == "local":
        principal = LocalPrincipal()
        set_request_identity("local", principal.user_id)
        return principal

    principal = _registered_principal(request, service)
    require_same_origin(request, service)
    context = service.validate_csrf(
        _session_token(request),
        request.headers.get("x-csrf-token", ""),
    )
    if context.user.id != principal.user_id:
        raise RegistrationRequiredError()
    return RegisteredPrincipal.from_context(context)


def _registered_principal(
    request: Request,
    service: AuthService,
) -> RegisteredPrincipal:
    try:
        context = service.authenticate(_session_token(request))
    except AuthenticationFailedError as exc:
        raise RegistrationRequiredError() from exc
    set_request_identity("user", context.user.id)
    return RegisteredPrincipal.from_context(context)


def _session_token(request: Request) -> str:
    settings: Settings = request.app.state.settings
    return request.cookies.get(settings.cookie_name, "")


def _request_origin(request: Request) -> str | None:
    return _normalize_origin(str(request.base_url))


def _origin_from_referer(value: str | None) -> str | None:
    if value is None:
        return None
    return _normalize_origin(value)


def _normalize_origin(value: str) -> str | None:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _secret_value(secret: object, *, name: str) -> str:
    getter = getattr(secret, "get_secret_value", None)
    if getter is None:
        raise ValueError(f"{name} 未配置")
    value = str(getter())
    if not value:
        raise ValueError(f"{name} 未配置")
    return value
