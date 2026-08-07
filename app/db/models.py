from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import UsageInfo


SessionStatus = Literal[
    "collecting",
    "need_more_facts",
    "ready",
    "escalate",
    "error",
]
AuditStatus = Literal["started", "ok", "error", "degraded"]


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    scenario_id: str | None = Field(default=None, max_length=100)
    facts: dict[str, Any] = Field(default_factory=dict)
    followup_round: int = Field(default=0, ge=0, le=2)
    status: SessionStatus = "collecting"
    jurisdiction: str | None = Field(default=None, max_length=100)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class SessionListRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    scenario_id: str | None = Field(default=None, max_length=100)
    status: SessionStatus
    first_user_message: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    session_id: str = Field(min_length=36, max_length=36)
    user_message: str = Field(min_length=1)
    facts: dict[str, Any] = Field(default_factory=dict)
    rule_matches: list[dict[str, Any]] = Field(default_factory=list)
    response: dict[str, Any] = Field(default_factory=dict)
    provider_name: str | None = Field(default=None, max_length=50)
    provider_model: str | None = Field(default=None, max_length=200)
    provider_request_id: str | None = Field(default=None, max_length=200)
    usage: UsageInfo = Field(default_factory=UsageInfo)
    created_at: datetime


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    audit_id: str = Field(min_length=36, max_length=36)
    session_id: str = Field(min_length=36, max_length=36)
    turn_id: str | None = Field(default=None, min_length=36, max_length=36)
    stage: str = Field(min_length=1, max_length=100)
    status: AuditStatus
    duration_ms: int = Field(default=0, ge=0)
    playbook_id: str | None = Field(default=None, max_length=100)
    playbook_version: str | None = Field(default=None, max_length=50)
    citations: list[str] = Field(default_factory=list)
    error_category: str | None = Field(default=None, max_length=100)
    created_at: datetime


class UsageDailyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: date
    client_hash: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=50)
    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    priced_request_count: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    updated_at: datetime


class RateLimitDailyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: date
    client_hash: str = Field(min_length=1, max_length=128)
    request_count: int = Field(ge=0)
    updated_at: datetime
