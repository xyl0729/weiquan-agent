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
    model_validator,
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


TurnKindResponse = Literal[
    "fact_collection",
    "initial_plan",
    "plan_update",
    "followup_answer",
    "new_case",
]


class NewCaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str | None = Field(default=None, max_length=100)
    label: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def fields_are_consistent(self) -> "NewCaseResponse":
        if (self.scenario_id is None) != (self.label is None):
            raise ValueError("新咨询场景与名称必须同时提供或同时为空")
        return self


class ReplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=800)
    suggested_actions: list[str] = Field(
        default_factory=list,
        max_length=3,
    )
    citation_refs: list[str] = Field(
        default_factory=list,
        max_length=3,
    )
    new_case: NewCaseResponse | None = None

    @field_validator("text")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("回复正文不能为空")
        return normalized

    @field_validator("suggested_actions", "citation_refs")
    @classmethod
    def list_items_are_valid(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("回复列表项不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("回复列表项不得重复")
        return normalized


class ConsultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    turn_id: UUID
    audit_id: UUID
    followup_round: int = Field(ge=0, le=2)
    can_ask_more: bool
    status: Literal["need_more_facts", "ready", "escalate"]
    turn_kind: TurnKindResponse | None = None
    verdict: VerdictResponse | None = None
    plan: PlanResponse | None = None
    reply: ReplyResponse | None = None
    questions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    citations: list[CitationResponse] = Field(default_factory=list)
    usage: UsageResponse

    @model_validator(mode="after")
    def turn_fields_are_consistent(self) -> "ConsultResponse":
        if self.turn_kind is None:
            if self.reply is not None:
                raise ValueError("旧版响应不得包含无类型的短回复")
            return self

        if self.turn_kind == "fact_collection":
            if (
                self.plan is not None
                or self.verdict is not None
                or self.reply is not None
            ):
                raise ValueError("事实收集轮次不得包含方案、判断或短回复")
            if not self.questions and not self.limitations:
                raise ValueError("事实收集轮次必须包含问题或限制说明")
        elif self.turn_kind in {"initial_plan", "plan_update"}:
            if (
                self.plan is None
                or self.verdict is None
                or self.reply is not None
            ):
                raise ValueError("方案轮次必须包含方案和判断，且不得包含短回复")
        elif self.turn_kind == "followup_answer":
            if (
                self.reply is None
                or self.plan is not None
                or self.verdict is not None
                or self.reply.new_case is not None
            ):
                raise ValueError("普通续问只能包含当前案件的短回复")
        elif (
            self.reply is None
            or self.reply.new_case is None
            or self.plan is not None
            or self.verdict is not None
        ):
            raise ValueError("分案轮次必须包含新咨询提示")

        if self.reply is not None:
            public_refs = [citation.ref for citation in self.citations]
            if self.reply.citation_refs != public_refs:
                raise ValueError("短回复引用必须与公开法条完全一致")
        return self


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
