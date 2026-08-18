from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import Settings
from app.db.session import SessionStore
from app.deps import (
    get_active_settings,
    get_provider_health_service,
)
from app.health.service import ProviderHealthService
from app.health.system import disk_capacity, temp_directory_ready
from app.jurisdiction.rules import JurisdictionRegistry
from app.playbooks.registry import PlaybookRegistry
from app.rendering.renderer import PlanRenderer
from app.retrieval.database import connect_database, get_metadata
from app.observability.metrics import (
    OperationalMetrics,
    attachment_temp_snapshot,
)


router = APIRouter(tags=["health"])


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/ready")
def ready(
    request: Request,
    settings: Settings = Depends(get_active_settings),
) -> JSONResponse:
    checks = _readiness_checks(request, settings)
    is_ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={
            "status": "ready" if is_ready else "not_ready",
            "checks": checks,
        },
    )


@router.get("/api/providers/deepseek/status")
def deepseek_status(
    health_service: ProviderHealthService = Depends(
        get_provider_health_service
    ),
) -> dict[str, Any]:
    state = health_service.status("deepseek")
    return {
        "provider": "deepseek",
        "status": state.status,
        "last_result_at": (
            state.last_result_at.isoformat()
            if state.last_result_at is not None
            else None
        ),
        "updated_at": state.updated_at.isoformat(),
    }


@router.get("/health")
def health(
    request: Request,
    settings: Settings = Depends(get_active_settings),
    health_service: ProviderHealthService = Depends(
        get_provider_health_service
    ),
) -> dict[str, Any]:
    checks = _readiness_checks(request, settings)
    if settings.llm_provider == "fake":
        checks["provider"] = "offline"
    else:
        checks["provider"] = health_service.status("deepseek").status

    pipeline = getattr(
        request.app.state,
        "consultation_pipeline",
        None,
    )
    controls_by_provider = getattr(
        pipeline,
        "usage_controls_by_provider",
        None,
    )
    controls = (
        controls_by_provider.get("deepseek")
        if isinstance(controls_by_provider, Mapping)
        else None
    )
    if controls is None:
        controls = getattr(pipeline, "usage_controls", None)
    if controls is not None and controls.enabled:
        checks["daily_spend_circuit"] = (
            "tripped"
            if controls.circuit.is_tripped()
            else "available"
        )
    else:
        checks["daily_spend_circuit"] = "inactive"

    checks["ocr"] = (
        "ok"
        if getattr(request.app.state, "ocr_ready", False)
        else "unavailable"
    )
    required = {
        key: value
        for key, value in checks.items()
        if key
        not in {
            "provider",
            "daily_spend_circuit",
            "ocr",
        }
    }
    return {
        "status": (
            "ok"
            if all(value == "ok" for value in required.values())
            else "degraded"
        ),
        "checks": checks,
    }


@router.get("/internal/metrics", include_in_schema=False)
def internal_metrics(
    request: Request,
    settings: Settings = Depends(get_active_settings),
    health_service: ProviderHealthService = Depends(
        get_provider_health_service
    ),
) -> dict[str, Any]:
    operational = getattr(
        request.app.state,
        "operational_metrics",
        None,
    )
    if not isinstance(operational, OperationalMetrics):
        operational = OperationalMetrics()
        request.app.state.operational_metrics = operational

    queues: dict[str, dict[str, int | str]] = {}
    for name in ("ocr", "deepseek"):
        executor = getattr(
            request.app.state,
            f"{name}_executor",
            None,
        )
        snapshot = getattr(executor, "snapshot", lambda: None)()
        if snapshot is not None:
            queues[name] = {
                "name": snapshot.name,
                "running": snapshot.running,
                "waiting": snapshot.waiting,
                "max_concurrency": snapshot.max_concurrency,
                "max_waiting": snapshot.max_waiting,
            }

    try:
        provider_state = health_service.status("deepseek")
        provider: dict[str, Any] = {
            "status": provider_state.status,
            "sample_count": provider_state.sample_count,
            "failure_count": provider_state.failure_count,
            "last_result_at": (
                provider_state.last_result_at.isoformat()
                if provider_state.last_result_at is not None
                else None
            ),
        }
    except Exception:
        provider = {
            "status": "unavailable",
            "sample_count": 0,
            "failure_count": 0,
            "last_result_at": None,
        }

    return {
        "queue": queues,
        "provider": provider,
        "mail": operational.snapshot("mail").to_dict(),
        "captcha": operational.snapshot("captcha").to_dict(),
        "attachment": attachment_temp_snapshot(
            settings.attachment_temp_path
        ).to_dict(),
    }


def _readiness_checks(
    request: Request,
    settings: Settings,
) -> dict[str, str]:
    checks: dict[str, str] = {}
    try:
        if settings.deployment_mode == "production":
            engine = getattr(request.app.state, "database_engine", None)
            if engine is None:
                from app.db.engine import create_database_engine

                engine = create_database_engine(settings)
                request.app.state.database_engine = engine
            with engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
        else:
            store = SessionStore(
                settings.database_path,
                ttl_hours=settings.session_ttl_hours,
            )
            store.initialize()
        checks["session_database"] = "ok"
    except Exception:
        checks["session_database"] = "unavailable"

    try:
        connection = connect_database(settings.statute_database_path)
        try:
            metadata = get_metadata(connection)
        finally:
            connection.close()
        checks["statute_database"] = (
            "ok"
            if metadata.get("placeholder_data") == "false"
            else "unverified"
        )
    except Exception:
        checks["statute_database"] = "unavailable"

    try:
        registry = PlaybookRegistry.from_directory(
            settings.playbooks_path
        )
        registry.verify_references(settings.statute_database_path)
        checks["playbooks"] = "ok"
    except Exception:
        checks["playbooks"] = "invalid"

    try:
        JurisdictionRegistry.from_path(settings.jurisdiction_path)
        checks["jurisdiction"] = "ok"
    except Exception:
        checks["jurisdiction"] = "invalid"

    try:
        PlanRenderer(settings.templates_path)
        checks["templates"] = "ok"
    except Exception:
        checks["templates"] = "invalid"

    checks["temporary_storage"] = (
        "ok" if temp_directory_ready(settings) else "unavailable"
    )
    if settings.deployment_mode != "production":
        checks["disk"] = "ok"
    else:
        try:
            checks["disk"] = (
                "ok" if disk_capacity(settings).available else "full"
            )
        except OSError:
            checks["disk"] = "unavailable"
    return checks
