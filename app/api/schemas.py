from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

from app.attachments.models import AttachmentTurnPublic
from app.jurisdiction.schema import TimeLimitResult


class RuntimeConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_mode: Literal["local_full_test", "account"]


class AttachmentConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed_text: str = Field(min_length=1)

    @field_validator("confirmed_text")
    @classmethod
    def confirmed_text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("确认文字不能为空")
        return normalized


class ConsultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = None
    message: str = Field(min_length=1, max_length=20000)
    jurisdiction: str | None = Field(default=None, max_length=100)
    attachment_ids: list[UUID] = Field(
        default_factory=list,
        max_length=3,
    )

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

    @field_validator("attachment_ids")
    @classmethod
    def attachment_ids_are_unique(
        cls,
        values: list[UUID],
    ) -> list[UUID]:
        if len(values) != len(set(values)):
            raise ValueError("附件 ID 不得重复")
        return values


class TrialStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captcha_token: SecretStr | None = None
    privacy_version: str = Field(default="", max_length=100)
    privacy_accepted: bool = False

    @field_validator("captcha_token")
    @classmethod
    def captcha_token_is_not_blank(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("captcha_token 不能为空")
        return value


class TrialConsultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = None
    message: str = Field(min_length=1, max_length=3000)
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


class TrialQuotaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    remaining_total: int = Field(ge=0, le=5)


class RegisteredQuotaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    remaining_daily: int = Field(ge=0, le=10)
    remaining_monthly: int = Field(ge=0, le=50)
    day_resets_at: datetime
    month_resets_at: datetime


class TrialStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_id: UUID
    quota: TrialQuotaResponse


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
    basis_scope: Literal["case_specific", "general"] = "case_specific"
    applicability_notice: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )


class JurisdictionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str | None = None
    name: str | None = None
    status: Literal["supported", "unknown", "local_data_missing"]
    small_claim_threshold_yuan: float | None = Field(default=None, gt=0)
    notices: list[str] = Field(default_factory=list)


class CommunicationGuideResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recipient: str = Field(min_length=1, max_length=200)
    channels: list[str] = Field(min_length=1, max_length=5)
    when_to_send: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=3000)
    after_sending: list[str] = Field(min_length=1, max_length=8)
    escalation: list[str] = Field(min_length=1, max_length=8)
    required_before_send: list[str] = Field(
        default_factory=list,
        max_length=8,
    )

    @field_validator(
        "channels",
        "after_sending",
        "escalation",
        "required_before_send",
    )
    @classmethod
    def communication_lists_are_clean(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("沟通指南列表项不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("沟通指南列表项不得重复")
        return normalized


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    evidence_now: list[str]
    actions: list[str]
    communication_text: str
    communication_guide: CommunicationGuideResponse | None = None
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


CoverageModeResponse = Literal[
    "formal",
    "unverified_guidance",
    "emergency_guidance",
]

RiskFlagResponse = Literal[
    "immediate_danger",
    "minor_harm",
    "urgent_medical",
    "suspected_crime",
    "fraud_loss",
    "evidence_loss",
]


class CoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: CoverageModeResponse
    topic_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]{1,99}$",
    )
    topic_label: str = Field(min_length=1, max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    playbook_id: str | None = Field(
        default=None,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]{1,99}$",
    )
    notice: str = Field(min_length=1, max_length=500)
    risk_flags: list[RiskFlagResponse] = Field(default_factory=list)

    @model_validator(mode="after")
    def fields_are_consistent(self) -> "CoverageResponse":
        if self.mode == "formal":
            if self.playbook_id is None:
                raise ValueError("正式覆盖必须关联 Playbook")
        elif self.playbook_id is not None:
            raise ValueError("指导模式不得关联正式 Playbook")
        if self.mode == "emergency_guidance" and not self.risk_flags:
            raise ValueError("紧急指导必须包含风险标志")
        if len(self.risk_flags) != len(set(self.risk_flags)):
            raise ValueError("风险标志不得重复")
        return self


class GuidanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direct_answer: str | None = Field(
        default=None,
        min_length=1,
        max_length=1200,
    )
    evidence_now: list[str] = Field(min_length=1, max_length=12)
    actions: list[str] = Field(min_length=1, max_length=12)
    communication_guide: CommunicationGuideResponse
    limitations: list[str] = Field(min_length=1, max_length=12)
    next_question: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )

    @field_validator("evidence_now", "actions", "limitations")
    @classmethod
    def guidance_lists_are_clean(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("指导列表项不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("指导列表项不得重复")
        return normalized


TurnKindResponse = Literal[
    "fact_collection",
    "initial_plan",
    "plan_update",
    "followup_answer",
    "new_case",
    "unverified_guidance",
    "emergency_guidance",
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

    text: str = Field(min_length=1, max_length=1200)
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
    coverage: CoverageResponse | None = None
    guidance: GuidanceResponse | None = None
    verdict: VerdictResponse | None = None
    plan: PlanResponse | None = None
    reply: ReplyResponse | None = None
    questions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    citations: list[CitationResponse] = Field(default_factory=list)
    attachments: list[AttachmentTurnPublic] = Field(
        default_factory=list,
        max_length=3,
    )
    usage: UsageResponse
    quota: TrialQuotaResponse | RegisteredQuotaResponse | None = None

    @model_validator(mode="after")
    def turn_fields_are_consistent(self) -> "ConsultResponse":
        if self.turn_kind is None:
            if self.reply is not None or self.guidance is not None:
                raise ValueError("旧版响应不得包含无类型的短回复")
            return self

        if self.turn_kind in {
            "unverified_guidance",
            "emergency_guidance",
        }:
            if (
                self.coverage is None
                or self.guidance is None
                or self.plan is not None
                or self.verdict is not None
                or self.reply is not None
                or self.questions
            ):
                raise ValueError(
                    "指导轮次必须包含覆盖与指导，且不得包含正式方案、"
                    "判断、短回复或追问列表"
                )
            if any(
                citation.basis_scope != "general"
                for citation in self.citations
            ):
                raise ValueError("指导轮次只能展示一般法律依据")
            if self.coverage.mode != self.turn_kind:
                raise ValueError("指导轮次类型必须与覆盖模式一致")
            if self.limitations != self.guidance.limitations:
                raise ValueError("指导限制必须与顶层限制完全一致")
            if (
                self.turn_kind == "emergency_guidance"
                and self.status != "escalate"
            ):
                raise ValueError("紧急指导必须使用升级状态")
            if self.can_ask_more != (
                self.guidance.next_question is not None
            ):
                raise ValueError("指导追问状态与关键问题不一致")
            return self

        if self.guidance is not None:
            raise ValueError("正式流程轮次不得包含指导结果")
        if (
            self.coverage is not None
            and self.coverage.mode != "formal"
            and not (
                self.turn_kind == "fact_collection"
                and self.coverage.mode == "unverified_guidance"
            )
        ):
            raise ValueError(
                "非指导轮次只能包含正式覆盖结果，"
                "安全澄清可保留未核验主题"
            )

        if self.turn_kind == "fact_collection":
            if (
                self.plan is not None
                or self.verdict is not None
                or self.citations
                or (
                    self.reply is not None
                    and (
                        self.reply.new_case is not None
                        or self.reply.citation_refs
                    )
                )
            ):
                raise ValueError(
                    "事实收集轮次不得包含方案、判断、新纠纷或法条"
                )
            if not self.questions and not self.limitations:
                raise ValueError("事实收集轮次必须包含问题或限制说明")
        elif self.turn_kind == "initial_plan":
            if (
                self.plan is None
                or self.verdict is None
                or self.reply is not None
            ):
                raise ValueError("方案轮次必须包含方案和判断，且不得包含短回复")
        elif self.turn_kind == "plan_update":
            if (
                self.plan is None
                or self.verdict is None
                or (
                    self.reply is not None
                    and self.reply.new_case is not None
                )
            ):
                raise ValueError("方案更新必须包含方案和判断")
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
            if (
                self.turn_kind == "plan_update"
                and not set(self.reply.citation_refs).issubset(public_refs)
            ):
                raise ValueError("方案更新短回复引用必须来自公开法条")
            if (
                self.turn_kind != "plan_update"
                and self.reply.citation_refs != public_refs
            ):
                raise ValueError("短回复引用必须与公开法条完全一致")
        return self


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,49}$")
    display_name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    available: bool
    unavailable_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    offline: bool
    is_default: bool

    @model_validator(mode="after")
    def availability_is_consistent(self) -> "ProviderResponse":
        if self.available and self.unavailable_reason is not None:
            raise ValueError("可用 Provider 不得包含不可用原因")
        if not self.available and self.unavailable_reason is None:
            raise ValueError("不可用 Provider 必须包含公开原因")
        return self


class ProviderListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    providers: list[ProviderResponse] = Field(min_length=1)

    @model_validator(mode="after")
    def providers_are_consistent(self) -> "ProviderListResponse":
        provider_ids = [provider.id for provider in self.providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("Provider ID 不得重复")
        if sum(provider.is_default for provider in self.providers) != 1:
            raise ValueError("必须有且仅有一个默认 Provider")
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


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=8, max_length=128)
    captcha_token: SecretStr | None = None
    privacy_version: str = Field(min_length=1, max_length=100)
    privacy_accepted: bool


class ResendVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    code: SecretStr

    @field_validator("code")
    @classmethod
    def code_is_six_ascii_digits(cls, value: SecretStr) -> SecretStr:
        raw_code = value.get_secret_value()
        if (
            len(raw_code) != 6
            or not raw_code.isascii()
            or not raw_code.isdigit()
        ):
            raise ValueError("验证码必须为 6 位数字")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: SecretStr


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr
    new_password: SecretStr = Field(min_length=8, max_length=128)


class AuthAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted"]


class CaptchaConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    scene_id: str
    prefix: str
    region: Literal["cn"] = "cn"


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    email: str
    role: Literal["user", "admin"]
    status: Literal["pending_verification", "active", "disabled"]
    verified_at: datetime | None = None
    created_at: datetime


class AuthUserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user: UserResponse
    quota: RegisteredQuotaResponse
    privacy_version: str = Field(min_length=1, max_length=100)
    privacy_acceptance_required: bool


class AuthCsrfResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    csrf_token: str = Field(min_length=32)


class PrivacyAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: Literal["consultation"] = "consultation"
    policy_version: str = Field(min_length=1, max_length=100)


class PrivacyPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1)
