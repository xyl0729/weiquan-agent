from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
import hashlib
import hmac
import logging
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.agent.errors import (
    CaseNoProgressError,
    CircuitTrippedError,
    ConsultationConflictError,
    DataIntegrityError,
    InvalidProviderError,
    NewWorkPausedError,
    ProviderError,
    RateLimitError,
    RequestInputError,
    SafeApplicationError,
    SessionNotFoundError,
    StorageUnavailableError,
)
from app.attachments.errors import (
    AttachmentError,
    AttachmentServiceUnavailableError,
)
from app.attachments.extractors import RapidOcrEngine
from app.api.admin import router as admin_router
from app.api.attachments import router as attachments_router
from app.api.auth import router as auth_router
from app.api.consult import router as consult_router
from app.api.health import router as health_router
from app.api.privacy import router as privacy_router
from app.api.providers import router as providers_router
from app.api.runtime import router as runtime_router
from app.api.sessions import router as sessions_router
from app.api.trial import router as trial_router
from app.auth.errors import AuthError
from app.auth.dependencies import initialize_auth_dependencies
from app.config import Settings, get_settings
from app.deletion.service import DeletionUnavailableError
from app.deps import (
    initialize_attachment_dependencies,
    initialize_trial_dependencies,
)
from app.limits.quota import QuotaExceededError
from app.observability.logging import configure_safe_json_logging
from app.observability.metrics import OperationalMetrics
from app.observability.request_context import RequestContextMiddleware


WEB_ROOT = Path(__file__).resolve().parent / "web"
CAPTCHA_SCRIPT_ORIGIN = "https://o.alicdn.com"
CAPTCHA_SDK_HOST_SUFFIX = ".captcha-sdk.aliyuncs.com"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=()"
    ),
}


def _content_security_policy(settings: Settings) -> str:
    script_sources = ["'self'"]
    connect_sources = ["'self'"]
    directives = [
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
    captcha_enabled = (
        settings.deployment_mode == "production"
        and settings.captcha_enabled
    )
    if captcha_enabled:
        captcha_origin = (
            f"https://{settings.captcha_prefix}{CAPTCHA_SDK_HOST_SUFFIX}"
        )
        script_sources.append(CAPTCHA_SCRIPT_ORIGIN)
        connect_sources.append(captcha_origin)
    directives.extend(
        [
            f"script-src {' '.join(script_sources)}",
            "style-src 'self'",
            "img-src 'self' data:",
            f"connect-src {' '.join(connect_sources)}",
            "font-src 'self'",
        ]
    )
    if captcha_enabled:
        directives.append(f"frame-src {captcha_origin}")
    return "; ".join(directives)


def _security_headers(settings: Settings) -> dict[str, str]:
    return {
        **SECURITY_HEADERS,
        "Content-Security-Policy": _content_security_policy(settings),
    }


def probe_ocr_readiness() -> bool:
    try:
        RapidOcrEngine()
    except Exception:
        return False
    return True


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings: Settings = application.state.settings
    if settings.deployment_mode == "production":
        configure_safe_json_logging(settings.logs_path)
    initialize_auth_dependencies(application)
    _, quota = initialize_trial_dependencies(application)
    quota.recover_stale(limit=100)
    try:
        _, service = initialize_attachment_dependencies(application)
        recovery_ready = True
        try:
            service.recover(limit=100)
            service.purge_expired(limit=100)
        except AttachmentServiceUnavailableError:
            recovery_ready = False
        application.state.ocr_ready = (
            probe_ocr_readiness() if recovery_ready else False
        )
        yield
    finally:
        engine = getattr(application.state, "database_engine", None)
        if engine is not None:
            engine.dispose()


def create_app(
    settings: Settings | None = None,
    *,
    pipeline: object | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    application = FastAPI(
        title="维权作战 Agent",
        version="0.1.0",
        description="信息整理与文书辅助工具，不构成法律意见。",
        lifespan=_lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
    application.add_middleware(
        RequestContextMiddleware,
        identity_secret=_request_log_identity_secret(active_settings),
        logger=logging.getLogger("weiquan.request"),
    )
    security_headers = _security_headers(active_settings)

    @application.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.update(security_headers)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = (
                "no-cache, max-age=0, must-revalidate"
            )
        return response

    application.state.settings = active_settings
    application.state.ocr_ready = False
    application.state.operational_metrics = OperationalMetrics()
    if pipeline is not None:
        application.state.consultation_pipeline = pipeline
        pipeline_store = getattr(pipeline, "store", None)
        if pipeline_store is not None:
            application.state.session_store = pipeline_store
    application.add_exception_handler(
        RequestValidationError,
        _request_validation_handler,
    )
    application.add_exception_handler(
        SafeApplicationError,
        _safe_error_handler,
    )
    application.include_router(health_router)
    application.include_router(runtime_router)
    application.include_router(auth_router)
    application.include_router(privacy_router)
    application.include_router(trial_router)
    application.include_router(attachments_router)
    application.include_router(consult_router)
    application.include_router(providers_router)
    application.include_router(sessions_router)
    application.include_router(admin_router)
    application.mount(
        "/static",
        StaticFiles(directory=WEB_ROOT),
        name="static",
    )

    @application.get("/", include_in_schema=False)
    def web_index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    return application


async def _request_validation_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del request, exc
    return _error_response(
        422,
        "request_validation",
        "请求字段无效",
    )


async def _safe_error_handler(
    request: Request,
    exc: SafeApplicationError,
) -> JSONResponse:
    del request
    if isinstance(exc, AuthError):
        status_code = exc.status_code
    elif isinstance(exc, AttachmentError):
        status_code = exc.status_code
    elif isinstance(
        exc,
        (InvalidProviderError, RequestInputError, SessionNotFoundError),
    ):
        status_code = 422
    elif isinstance(exc, (RateLimitError, QuotaExceededError)):
        status_code = 429
    elif isinstance(
        exc,
        (CaseNoProgressError, ConsultationConflictError),
    ):
        status_code = 409
    elif isinstance(
        exc,
        (
            ProviderError,
            CircuitTrippedError,
            StorageUnavailableError,
            NewWorkPausedError,
            DeletionUnavailableError,
        ),
    ):
        status_code = 503
    elif isinstance(exc, DataIntegrityError):
        status_code = 500
    else:
        status_code = 500
    return _error_response(
        status_code,
        exc.code,
        exc.safe_message,
    )


def _error_response(
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": message,
            }
        },
    )


def _request_log_identity_secret(settings: Settings) -> bytes:
    getter = getattr(settings.ip_hmac_secret, "get_secret_value", None)
    if getter is None:
        seed = b"weiquan-local-request-log-seed-v1"
    else:
        seed = str(getter()).encode("utf-8")
    return hmac.new(
        seed,
        b"weiquan-request-log-identity-v1",
        hashlib.sha256,
    ).digest()


app = create_app()
