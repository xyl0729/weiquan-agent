from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, status

from app.admin.service import (
    AdminService,
    InMemoryAdminAuditStore,
    PostgresAdminAuditStore,
)
from app.agent.errors import (
    DataIntegrityError,
    ProviderError,
    StorageUnavailableError,
)
from app.agent.pipeline import ConsultationPipeline
from app.attachments.service import AttachmentService
from app.attachments.store import AttachmentStore
from app.config import Settings
from app.db.contracts import (
    ApplicationStore,
    AttachmentRepository,
    SessionRepository,
)
from app.db.engine import create_database_engine
from app.db.postgres import PostgresApplicationStore
from app.db.session import SessionStore
from app.deletion.service import AgeCliEncryptor, DeletionService
from app.execution.bounded import BoundedExecutor
from app.health.service import ProviderHealthService
from app.history.service import SessionHistoryService
from app.integrations.oss import AliyunPrivateObjectStore
from app.jurisdiction.rules import JurisdictionRegistry
from app.limits.circuit import DailySpendCircuit
from app.limits.rate_limit import DailyRateLimiter
from app.limits.usage import (
    ProviderUsageControls,
    UsagePricer,
    UsageTracker,
)
from app.limits.reservations import (
    InMemoryQuotaStore,
    PostgresQuotaStore,
    QuotaService,
)
from app.playbooks.registry import PlaybookRegistry
from app.providers.catalog import ProviderResolver
from app.providers.health import (
    InMemoryProviderHealthStore,
    PostgresProviderHealthStore,
)
from app.rendering.renderer import PlanRenderer
from app.security.network import client_ip_from_request
from app.trial.identity import (
    InMemoryTrialIdentityStore,
    PostgresTrialIdentityStore,
    TrialIdentityManager,
    TrialTokenCodec,
)
from app.trial.conversations import InMemoryTrialConversationStore


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
    return client_ip_from_request(request)


def get_active_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_store(request: Request) -> SessionRepository:
    return _get_session_store(request.app)


def _get_session_store(application: FastAPI) -> SessionRepository:
    existing = getattr(application.state, "session_store", None)
    if isinstance(existing, SessionRepository):
        return existing

    settings = application.state.settings
    if settings.deployment_mode == "production":
        return _get_application_store(application)

    pipeline = getattr(
        application.state,
        "consultation_pipeline",
        None,
    )
    pipeline_store = getattr(pipeline, "store", None)
    if isinstance(pipeline_store, SessionRepository):
        application.state.session_store = pipeline_store
        return pipeline_store

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


def _get_application_store(application: FastAPI) -> ApplicationStore:
    existing = getattr(application.state, "application_store", None)
    if isinstance(existing, ApplicationStore):
        application.state.session_store = existing
        application.state.attachment_store = existing
        return existing

    settings = application.state.settings
    engine = getattr(application.state, "database_engine", None)
    if engine is None:
        engine = create_database_engine(settings)
        application.state.database_engine = engine
    store = PostgresApplicationStore(
        engine,
        retention_days=getattr(
            settings,
            "session_retention_days",
            30,
        ),
        attachment_draft_ttl_seconds=(
            settings.attachment_draft_ttl_seconds
        ),
    )
    application.state.application_store = store
    application.state.session_store = store
    application.state.attachment_store = store
    return store


def initialize_trial_dependencies(
    application: FastAPI,
) -> tuple[TrialIdentityManager, QuotaService]:
    existing_manager = getattr(
        application.state,
        "trial_identity_manager",
        None,
    )
    existing_quota = getattr(application.state, "quota_service", None)
    if (
        isinstance(existing_manager, TrialIdentityManager)
        and isinstance(existing_quota, QuotaService)
    ):
        return existing_manager, existing_quota

    settings: Settings = application.state.settings
    from app.auth.dependencies import initialize_auth_dependencies

    auth = initialize_auth_dependencies(application)
    captcha = getattr(application.state, "auth_captcha", auth.captcha)
    application.state.auth_captcha = captcha

    if settings.deployment_mode == "production":
        engine = getattr(application.state, "database_engine", None)
        if engine is None:
            engine = create_database_engine(settings)
            application.state.database_engine = engine
        identity_store = PostgresTrialIdentityStore(engine)
        quota_store = PostgresQuotaStore(engine)
        session_secret = _required_secret(
            settings.session_secret,
            name="SESSION_SECRET",
        )
        ip_secret = _required_secret(
            settings.ip_hmac_secret,
            name="IP_HMAC_SECRET",
        )
    else:
        identity_store = InMemoryTrialIdentityStore()
        quota_store = InMemoryQuotaStore()
        session_secret = hashlib.sha256(
            b"weiquan-local-trial-session-v1"
        ).digest()
        ip_secret = hashlib.sha256(
            b"weiquan-local-trial-ip-v1"
        ).digest()

    trial_token_secret = hmac.new(
        session_secret,
        b"weiquan-trial-token-key-v1",
        hashlib.sha256,
    ).digest()
    manager = TrialIdentityManager(
        store=identity_store,
        captcha=captcha,
        policy_version=settings.privacy_policy_version,
        token_codec=TrialTokenCodec(trial_token_secret),
        ip_hmac_secret=ip_secret,
        identity_ttl=timedelta(
            seconds=settings.trial_cookie_ttl_seconds
        ),
        ip_grant_ttl=timedelta(
            days=settings.trial_ip_grant_ttl_days
        ),
        pending_ip_grant_ttl=timedelta(
            minutes=settings.trial_pending_ip_grant_ttl_minutes
        ),
        max_identities_per_ip=settings.trial_max_identities_per_ip,
    )
    quota = QuotaService(
        quota_store,
        trial_total_limit=settings.trial_total_quota,
        trial_global_daily_limit=settings.trial_global_daily_quota,
        registered_daily_limit=settings.registered_daily_quota,
        registered_monthly_limit=settings.registered_monthly_quota,
        stale_after=timedelta(
            seconds=settings.quota_reservation_stale_seconds
        ),
    )
    application.state.trial_identity_store = identity_store
    application.state.trial_identity_manager = manager
    application.state.quota_store = quota_store
    application.state.quota_service = quota
    return manager, quota


def get_trial_identity_manager(
    request: Request,
) -> TrialIdentityManager:
    manager, _ = initialize_trial_dependencies(request.app)
    return manager


def get_quota_service(request: Request) -> QuotaService:
    _, quota = initialize_trial_dependencies(request.app)
    return quota


def get_trial_conversation_store(
    request: Request,
) -> InMemoryTrialConversationStore:
    existing = getattr(
        request.app.state,
        "trial_conversation_store",
        None,
    )
    if isinstance(existing, InMemoryTrialConversationStore):
        return existing
    settings = get_active_settings(request)
    store = InMemoryTrialConversationStore(
        ttl_seconds=settings.trial_conversation_ttl_seconds,
        capacity=settings.trial_conversation_capacity,
        max_turns=settings.trial_conversation_max_turns,
    )
    request.app.state.trial_conversation_store = store
    return store


def get_provider_health_service(
    request: Request,
) -> ProviderHealthService:
    _, _, health = initialize_execution_dependencies(request.app)
    return health


def get_admin_service(request: Request) -> AdminService:
    existing = getattr(request.app.state, "admin_service", None)
    if isinstance(existing, AdminService):
        return existing

    from app.auth.dependencies import initialize_auth_dependencies

    auth = initialize_auth_dependencies(request.app)
    quota = get_quota_service(request)
    health = get_provider_health_service(request)
    settings: Settings = request.app.state.settings
    if settings.deployment_mode == "production":
        engine = getattr(request.app.state, "database_engine", None)
        if engine is None:
            engine = create_database_engine(settings)
            request.app.state.database_engine = engine
        audit_store = PostgresAdminAuditStore(engine)
    else:
        audit_store = InMemoryAdminAuditStore()
    service = AdminService(
        auth_store=auth.store,
        quota_service=quota,
        provider_health=health,
        audit_store=audit_store,
    )
    request.app.state.admin_audit_store = audit_store
    request.app.state.admin_service = service
    return service


def _required_secret(secret: object, *, name: str) -> bytes:
    getter = getattr(secret, "get_secret_value", None)
    if getter is None:
        raise ValueError(f"{name} 未配置")
    value = str(getter()).encode("utf-8")
    if len(value) < 32:
        raise ValueError(f"{name} 未配置")
    return value


def _required_credential(secret: object, *, name: str) -> str:
    getter = getattr(secret, "get_secret_value", None)
    if getter is None:
        raise ValueError(f"{name} 未配置")
    value = str(getter())
    if not value:
        raise ValueError(f"{name} 未配置")
    return value


def initialize_execution_dependencies(
    application: FastAPI,
) -> tuple[BoundedExecutor, BoundedExecutor, ProviderHealthService]:
    existing_ocr = getattr(application.state, "ocr_executor", None)
    existing_deepseek = getattr(
        application.state,
        "deepseek_executor",
        None,
    )
    existing_health = getattr(
        application.state,
        "provider_health_service",
        None,
    )
    if (
        isinstance(existing_ocr, BoundedExecutor)
        and isinstance(existing_deepseek, BoundedExecutor)
        and isinstance(existing_health, ProviderHealthService)
    ):
        return existing_ocr, existing_deepseek, existing_health

    settings: Settings = application.state.settings
    ocr = BoundedExecutor(
        name="ocr",
        max_concurrency=settings.ocr_max_concurrency,
        max_waiting=settings.ocr_max_waiting,
    )
    deepseek = BoundedExecutor(
        name="deepseek",
        max_concurrency=settings.deepseek_max_concurrency,
        max_waiting=settings.deepseek_max_waiting,
        wait_timeout_seconds=(
            settings.deepseek_queue_timeout_seconds
        ),
    )
    if settings.deployment_mode == "production":
        engine = getattr(application.state, "database_engine", None)
        if engine is None:
            engine = create_database_engine(settings)
            application.state.database_engine = engine
        health_store = PostgresProviderHealthStore(engine)
    else:
        health_store = InMemoryProviderHealthStore()
    health = ProviderHealthService(health_store)
    application.state.ocr_executor = ocr
    application.state.deepseek_executor = deepseek
    application.state.provider_health_store = health_store
    application.state.provider_health_service = health
    return ocr, deepseek, health


def initialize_attachment_dependencies(
    application: FastAPI,
) -> tuple[AttachmentRepository, AttachmentService]:
    settings: Settings = application.state.settings
    ocr_executor, _, _ = initialize_execution_dependencies(application)
    if settings.deployment_mode == "production":
        store = _get_application_store(application)
        existing_service = getattr(
            application.state,
            "attachment_service",
            None,
        )
        if (
            isinstance(existing_service, AttachmentService)
            and existing_service.store is store
        ):
            return store, existing_service
        service = _build_attachment_service(
            store,
            settings=settings,
            executor=ocr_executor,
        )
        application.state.attachment_service = service
        return store, service

    pipeline = getattr(
        application.state,
        "consultation_pipeline",
        None,
    )
    pipeline_store = getattr(pipeline, "attachments", None)
    existing_store = getattr(
        application.state,
        "attachment_store",
        None,
    )
    if isinstance(pipeline_store, AttachmentStore):
        store = pipeline_store
        application.state.attachment_store = store
    elif isinstance(existing_store, AttachmentStore):
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
        service = _build_attachment_service(
            store,
            settings=settings,
            executor=ocr_executor,
        )
        application.state.attachment_service = service
    return store, service


def _build_attachment_service(
    store: AttachmentRepository,
    *,
    settings: Settings,
    executor: BoundedExecutor,
) -> AttachmentService:
    return AttachmentService(
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
        executor=executor,
    )


def get_attachment_store(request: Request) -> AttachmentRepository:
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
        deletion_service=initialize_deletion_dependencies(request.app),
    )


def build_deletion_service(
    settings: Settings,
    repository: PostgresApplicationStore,
) -> DeletionService:
    access_key_id = _required_credential(
        settings.aliyun_access_key_id,
        name="ALIYUN_ACCESS_KEY_ID",
    )
    access_key_secret = _required_credential(
        settings.aliyun_access_key_secret,
        name="ALIYUN_ACCESS_KEY_SECRET",
    )
    object_store = AliyunPrivateObjectStore.from_credentials(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        endpoint=settings.oss_endpoint,
        bucket_name=settings.oss_bucket,
    )
    return DeletionService(
        repository=repository,
        encryptor=AgeCliEncryptor(
            settings.deletion_manifest_recipient
        ),
        object_store=object_store,
    )


def initialize_deletion_dependencies(
    application: FastAPI,
) -> DeletionService | None:
    settings: Settings = application.state.settings
    if settings.deployment_mode != "production":
        return None
    existing = getattr(application.state, "deletion_service", None)
    if isinstance(existing, DeletionService):
        return existing
    repository = _get_application_store(application)
    service = build_deletion_service(settings, repository)
    application.state.deletion_service = service
    return service


def get_consultation_pipeline(request: Request) -> ConsultationPipeline:
    existing = getattr(request.app.state, "consultation_pipeline", None)
    if existing is not None:
        return existing

    settings = get_active_settings(request)
    try:
        store = get_session_store(request)
        attachments, _ = initialize_attachment_dependencies(
            request.app
        )
        registry = PlaybookRegistry.from_directory(
            settings.playbooks_path
        )
        registry.verify_references(settings.statute_database_path)
        request.app.state.playbook_registry = registry
        jurisdictions = JurisdictionRegistry.from_path(
            settings.jurisdiction_path
        )
        renderer = PlanRenderer(settings.templates_path)
        _, deepseek_executor, provider_health = (
            initialize_execution_dependencies(request.app)
        )
        provider_resolver = ProviderResolver.from_settings(
            settings,
            executor=deepseek_executor,
            health_service=provider_health,
        )
        usage_controls: dict[str, ProviderUsageControls] = {}
        deepseek = provider_resolver.catalog.get("deepseek")
        if (
            settings.deployment_mode != "production"
            and deepseek is not None
            and deepseek.available
        ):
            usage_controls["deepseek"] = ProviderUsageControls(
                enabled=True,
                provider="deepseek",
                rate_limiter=DailyRateLimiter(
                    store,
                    limit=settings.free_quota_per_day,
                ),
                circuit=DailySpendCircuit(
                    store,
                    provider="deepseek",
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
        attachments=attachments,
        registry=registry,
        provider_resolver=provider_resolver,
        jurisdictions=jurisdictions,
        renderer=renderer,
        usage_controls=usage_controls,
    )
    request.app.state.consultation_pipeline = pipeline
    return pipeline
