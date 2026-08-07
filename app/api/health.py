from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.config import Settings
from app.db.session import SessionStore
from app.deps import get_active_settings
from app.jurisdiction.rules import JurisdictionRegistry
from app.playbooks.registry import PlaybookRegistry
from app.rendering.renderer import PlanRenderer
from app.retrieval.database import connect_database, get_metadata


router = APIRouter(tags=["health"])


@router.get("/health")
def health(
    request: Request,
    settings: Settings = Depends(get_active_settings),
) -> dict[str, Any]:
    checks: dict[str, str] = {}

    try:
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

    if settings.llm_provider == "fake":
        checks["provider"] = "offline"
    elif settings.deepseek_api_key is None:
        checks["provider"] = "missing_key"
    else:
        checks["provider"] = "configured"

    pipeline = getattr(
        request.app.state,
        "consultation_pipeline",
        None,
    )
    controls = getattr(pipeline, "usage_controls", None)
    if controls is not None and controls.enabled:
        checks["daily_spend_circuit"] = (
            "tripped"
            if controls.circuit.is_tripped()
            else "available"
        )
    else:
        checks["daily_spend_circuit"] = "inactive"

    healthy_values = {
        "ok",
        "offline",
        "configured",
        "available",
        "inactive",
    }
    overall = (
        "ok"
        if all(value in healthy_values for value in checks.values())
        else "degraded"
    )
    return {
        "status": overall,
        "checks": checks,
    }
