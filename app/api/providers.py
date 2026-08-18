from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.schemas import ProviderListResponse, ProviderResponse
from app.config import Settings
from app.deps import get_active_settings
from app.providers.catalog import ProviderCatalog


router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("", response_model=ProviderListResponse)
def list_providers(
    settings: Settings = Depends(get_active_settings),
) -> ProviderListResponse:
    catalog = ProviderCatalog.from_settings(settings)
    return ProviderListResponse(
        providers=[
            ProviderResponse.model_validate(
                entry.model_dump(mode="json")
            )
            for entry in catalog.public_entries
        ]
    )
