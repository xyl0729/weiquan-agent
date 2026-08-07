from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
)

from app.jurisdiction.schema import TimeLimitResult


class ConsultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = None
    message: str = Field(min_length=1, max_length=20000)
    jurisdiction: str | None = Field(default=None, max_length=100)

    @field_validator("message")
    @classmethod
    def message_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message 不能为空")
        return normalized

    @field_validator("jurisdiction")
    @classmethod
    def normalize_jurisdiction(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class VerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    label: str
    status: Literal["need_more_facts", "ready", "escalate"]
    rule_ids: list[str]
    key_point: str


class CitationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    law_name: str
    article_no: str
    content: str
    effective_date: date
    source_url: HttpUrl


class JurisdictionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str | None = None
    name: str | None = None
    status: Literal["supported", "unknown", "local_data_missing"]
    small_claim_threshold_yuan: float | None = Field(default=None, gt=0)
    notices: list[str] = Field(default_factory=list)


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    evidence_now: list[str]
    actions: list[str]
    communication_text: str
    limitations: list[str]
    time_limit: TimeLimitResult | None = None
    jurisdiction: JurisdictionResponse
    rendered_text: str | None = None
    evidence_request_text: str | None = None


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    request_id: str | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class ConsultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    turn_id: UUID
    audit_id: UUID
    followup_round: int = Field(ge=0, le=2)
    can_ask_more: bool
    status: Literal["need_more_facts", "ready", "escalate"]
    verdict: VerdictResponse | None = None
    plan: PlanResponse | None = None
    questions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    citations: list[CitationResponse] = Field(default_factory=list)
    usage: UsageResponse


HistoryStatus = Literal["need_more_facts", "ready", "escalate"]


class SessionSummaryResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        from_attributes=True,
    )

    session_id: UUID
    title: str = Field(min_length=1, max_length=25)
    scenario_id: str | None = Field(default=None, max_length=100)
    status: HistoryStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class SessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sessions: list[SessionSummaryResponse]


class SessionTurnResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        from_attributes=True,
    )

    turn_id: UUID
    user_message: str = Field(min_length=1)
    response: ConsultResponse
    created_at: datetime


class SessionDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session: SessionSummaryResponse
    turns: list[SessionTurnResponse]


class SafeErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class SafeErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: SafeErrorDetail
