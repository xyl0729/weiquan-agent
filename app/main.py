from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.agent.errors import (
    CircuitTrippedError,
    DataIntegrityError,
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
from app.api.attachments import router as attachments_router
from app.api.consult import router as consult_router
from app.api.health import router as health_router
from app.api.sessions import router as sessions_router
from app.config import Settings, get_settings
from app.deps import initialize_attachment_dependencies


WEB_ROOT = Path(__file__).resolve().parent / "web"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=()"
    ),
}


def probe_ocr_readiness() -> bool:
    try:
        RapidOcrEngine()
    except Exception:
        return False
    return True


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    store, service = initialize_attachment_dependencies(application)
    recovery_ready = True
    try:
        service.recover(limit=100)
    except AttachmentServiceUnavailableError:
        recovery_ready = False
    store.purge_expired(limit=100)
    application.state.ocr_ready = (
        probe_ocr_readiness() if recovery_ready else False
    )
    yield


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
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @application.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    application.state.settings = active_settings
    application.state.ocr_ready = False
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
    application.include_router(attachments_router)
    application.include_router(consult_router)
    application.include_router(sessions_router)
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
    if isinstance(exc, AttachmentError):
        status_code = exc.status_code
    elif isinstance(exc, (RequestInputError, SessionNotFoundError)):
        status_code = 422
    elif isinstance(exc, RateLimitError):
        status_code = 429
    elif isinstance(
        exc,
        (ProviderError, CircuitTrippedError, StorageUnavailableError),
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


app = create_app()
