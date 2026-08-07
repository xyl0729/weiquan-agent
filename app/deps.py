from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, status

from app.agent.errors import (
    DataIntegrityError,
    ProviderError,
    StorageUnavailableError,
)
from app.agent.pipeline import ConsultationPipeline
from app.attachments.service import AttachmentService
from app.attachments.store import AttachmentStore
from app.config import Settings
from app.db.session import SessionStore
from app.history.service import SessionHistoryService
from app.jurisdiction.rules import JurisdictionRegistry
from app.limits.circuit import DailySpendCircuit
from app.limits.rate_limit import DailyRateLimiter
from app.limits.usage import (
    ProviderUsageControls,
    UsagePricer,
    UsageTracker,
)
from app.playbooks.registry import PlaybookRegistry
from app.providers.factory import create_provider
from app.rendering.renderer import PlanRenderer


class CircuitState(Protocol):
    def is_tripped(self) -> bool: ...


class DailyRateState(Protocol):
    def exceeded(self, client_ip: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class Credential:
    key: str
    source: str
    metered: bool


def resolve_credential(
    *,
    user_key: str | None,
    client_ip: str,
    settings: Settings,
    circuit: CircuitState,
    ratelimit: DailyRateState,
) -> Credential:
    normalized_user_key = (user_key or "").strip()
    if normalized_user_key:
        return Credential(
            key=normalized_user_key,
            source="user",
            metered=False,
        )

    if settings.key_mode == "byok":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先填写你的 API Key",
        )

    if circuit.is_tripped():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="今日免费额度已用完，可填写自己的 API Key 继续使用",
        )

    if ratelimit.exceeded(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="今日免费次数已用完，可填写自己的 API Key 继续使用",
        )

    if settings.server_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="服务端未配置 API Key",
        )

    return Credential(
        key=settings.server_api_key.get_secret_value(),
        source="server",
        metered=True,
    )


def request_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def get_active_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_store(request: Request) -> SessionStore:
    return _get_session_store(request.app)


def _get_session_store(application: FastAPI) -> SessionStore:
    existing = getattr(application.state, "session_store", None)
    if isinstance(existing, SessionStore):
        return existing

    pipeline = getattr(
        application.state,
        "consultation_pipeline",
        None,
    )
    pipeline_store = getattr(pipeline, "store", None)
    if isinstance(pipeline_store, SessionStore):
        application.state.session_store = pipeline_store
        return pipeline_store

    settings = application.state.settings
    try:
        store = SessionStore(
            settings.database_path,
            ttl_hours=settings.session_ttl_hours,
        )
        store.initialize()
    except (OSError, ValueError) as exc:
        raise StorageUnavailableError() from exc
    application.state.session_store = store
    return store


def initialize_attachment_dependencies(
    application: FastAPI,
) -> tuple[AttachmentStore, AttachmentService]:
    settings: Settings = application.state.settings
    existing_store = getattr(
        application.state,
        "attachment_store",
        None,
    )
    if isinstance(existing_store, AttachmentStore):
        store = existing_store
    else:
        store = AttachmentStore(
            _get_session_store(application),
            draft_ttl_seconds=settings.attachment_draft_ttl_seconds,
        )
        application.state.attachment_store = store

    existing_service = getattr(
        application.state,
        "attachment_service",
        None,
    )
    if (
        isinstance(existing_service, AttachmentService)
        and existing_service.store is store
    ):
        service = existing_service
    else:
        service = AttachmentService(
            store,
            temp_dir=settings.attachment_temp_path,
            max_file_bytes=settings.max_attachment_bytes,
            max_pdf_pages=settings.max_attachment_pdf_pages,
            max_image_pixels=settings.max_attachment_image_pixels,
            max_extracted_chars=settings.max_attachment_extracted_chars,
            low_confidence_threshold=(
                settings.attachment_low_confidence_threshold
            ),
            extraction_timeout_seconds=(
                settings.attachment_extraction_timeout_seconds
            ),
        )
        application.state.attachment_service = service
    return store, service


def get_attachment_store(request: Request) -> AttachmentStore:
    store, _ = initialize_attachment_dependencies(request.app)
    return store


def get_attachment_service(request: Request) -> AttachmentService:
    _, service = initialize_attachment_dependencies(request.app)
    return service


def get_session_history_service(
    request: Request,
) -> SessionHistoryService:
    pipeline = getattr(
        request.app.state,
        "consultation_pipeline",
        None,
    )
    registry = getattr(pipeline, "registry", None)
    if not isinstance(registry, PlaybookRegistry):
        registry = getattr(
            request.app.state,
            "playbook_registry",
            None,
        )
    if not isinstance(registry, PlaybookRegistry):
        settings = get_active_settings(request)
        try:
            registry = PlaybookRegistry.from_directory(
                settings.playbooks_path
            )
        except (OSError, ValueError) as exc:
            raise DataIntegrityError(
                "dependency_integrity_failed",
                "本地咨询依赖未通过完整性检查",
            ) from exc
        request.app.state.playbook_registry = registry
    return SessionHistoryService(
        get_session_store(request),
        registry,
    )


def get_consultation_pipeline(request: Request) -> ConsultationPipeline:
    existing = getattr(request.app.state, "consultation_pipeline", None)
    if existing is not None:
        return existing

    settings = get_active_settings(request)
    try:
        store = get_session_store(request)
        registry = PlaybookRegistry.from_directory(
            settings.playbooks_path
        )
        registry.verify_references(settings.statute_database_path)
        request.app.state.playbook_registry = registry
        jurisdictions = JurisdictionRegistry.from_path(
            settings.jurisdiction_path
        )
        renderer = PlanRenderer(settings.templates_path)
        provider = create_provider(settings)
        usage_controls = ProviderUsageControls(
            enabled=provider.name == "deepseek",
            provider=provider.name,
            rate_limiter=DailyRateLimiter(
                store,
                limit=settings.free_quota_per_day,
            ),
            circuit=DailySpendCircuit(
                store,
                provider=provider.name,
                limit_usd=settings.daily_spend_limit_usd,
            ),
            pricer=UsagePricer(
                input_per_million=(
                    settings.deepseek_price_input_per_million
                ),
                output_per_million=(
                    settings.deepseek_price_output_per_million
                ),
            ),
            tracker=UsageTracker(store),
        )
    except ProviderError:
        raise
    except (OSError, ValueError) as exc:
        raise DataIntegrityError(
            "dependency_integrity_failed",
            "本地咨询依赖未通过完整性检查",
        ) from exc
    except Exception as exc:
        raise StorageUnavailableError() from exc

    pipeline = ConsultationPipeline(
        settings=settings,
        store=store,
        registry=registry,
        provider=provider,
        jurisdictions=jurisdictions,
        renderer=renderer,
        usage_controls=usage_controls,
    )
    request.app.state.consultation_pipeline = pipeline
    return pipeline
