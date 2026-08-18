from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.auth.models import UserStatus
from app.limits.quota import RegisteredQuotaStatus
from app.providers.health import (
    ProviderHealthStatus,
    ProviderOutcome,
)


AdminAction = Literal["revoke_sessions", "disable_user"]
AdminActionResultStatus = Literal["succeeded", "not_found"]


class AdminAccountDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1, max_length=100)
    status: UserStatus
    email_verified: bool
    created_at: datetime
    quota: RegisteredQuotaStatus


class AdminProviderDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=50)
    status: ProviderHealthStatus
    sample_count: int = Field(ge=0, le=100)
    success_count: int = Field(ge=0, le=100)
    error_categories: list[ProviderOutcome]
    last_result_at: datetime | None = None


class AdminDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accounts: list[AdminAccountDiagnostics]
    provider: AdminProviderDiagnostics


class AdminAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    admin_id: str = Field(min_length=1, max_length=100)
    target_user_id: str = Field(min_length=1, max_length=100)
    action: AdminAction
    occurred_at: datetime
    result: AdminActionResultStatus


class AdminActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: AdminAction
    target_user_id: str = Field(min_length=1, max_length=100)
    result: AdminActionResultStatus

