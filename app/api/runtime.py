from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.schemas import RuntimeConfigResponse
from app.config import Settings
from app.deps import get_active_settings


router = APIRouter(prefix="/api", tags=["runtime"])


@router.get("/runtime-config", response_model=RuntimeConfigResponse)
def runtime_config(
    settings: Settings = Depends(get_active_settings),
) -> RuntimeConfigResponse:
    identity_mode = (
        "local_full_test"
        if settings.deployment_mode == "local"
        else "account"
    )
    return RuntimeConfigResponse(identity_mode=identity_mode)
