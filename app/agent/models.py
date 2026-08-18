from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


TurnKind = Literal[
    "fact_collection",
    "initial_plan",
    "plan_update",
    "followup_answer",
    "new_case",
    "unverified_guidance",
    "emergency_guidance",
]

CoverageMode = Literal[
    "formal",
    "unverified_guidance",
    "emergency_guidance",
]

TurnIntent = Literal[
    "question",
    "new_fact",
    "stated_goal",
    "completed_action",
    "continue_case",
    "correction",
    "new_case",
]

RiskFlag = Literal[
    "immediate_danger",
    "minor_harm",
    "urgent_medical",
    "suspected_crime",
    "fraud_loss",
    "evidence_loss",
]


class UsageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def fill_or_check_total(self) -> "UsageInfo":
        calculated = self.input_tokens + self.output_tokens
        if self.total_tokens == 0 and calculated:
            object.__setattr__(self, "total_tokens", calculated)
        elif self.total_tokens and self.total_tokens < calculated:
            raise ValueError("total_tokens 不能小于输入和输出 token 之和")
        return self


class CaseScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list)
    slot_definitions: dict[str, dict[str, Any]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def aliases_are_unique(self) -> "CaseScenario":
        normalized = [alias.strip() for alias in self.aliases]
        if any(not alias for alias in normalized):
            raise ValueError("场景别名不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("场景别名不得重复")
        return self


class CaseAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(pattern=r"^A[1-9][0-9]*$", max_length=10)
    text: str = Field(min_length=1, max_length=1000)


class CaseCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=3, max_length=200)
    law_name: str = Field(min_length=1, max_length=200)
    article_no: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)
    purpose: str | None = Field(default=None, min_length=1, max_length=300)


class LockedCaseContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict_label: str = Field(min_length=1, max_length=200)
    key_point: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=1000)
    actions: list[CaseAction] = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    citations: list[CaseCitation] = Field(min_length=1)

    @model_validator(mode="after")
    def references_are_unique(self) -> "LockedCaseContext":
        action_refs = [item.ref for item in self.actions]
        citation_refs = [item.ref for item in self.citations]
        if len(action_refs) != len(set(action_refs)):
            raise ValueError("案件动作引用不得重复")
        if len(citation_refs) != len(set(citation_refs)):
            raise ValueError("案件法条引用不得重复")
        return self


class RecentCaseTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_message: str = Field(min_length=1, max_length=500)
    turn_kind: TurnKind
    assistant_reply: str | None = Field(
        default=None,
        min_length=1,
        max_length=1200,
    )


class CaseContinuationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current_scenario: CaseScenario
    registered_scenarios: list[CaseScenario] = Field(min_length=1)
    existing_facts: dict[str, Any] = Field(default_factory=dict)
    locked_case: LockedCaseContext
    recent_turns: list[RecentCaseTurn] = Field(
        default_factory=list,
        max_length=4,
    )

    @model_validator(mode="after")
    def context_is_bounded_and_consistent(
        self,
    ) -> "CaseContinuationContext":
        scenario_ids = [item.id for item in self.registered_scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("正式场景目录不得重复")
        if self.current_scenario.id not in set(scenario_ids):
            raise ValueError("当前场景必须存在于正式场景目录")
        history_length = sum(
            len(turn.user_message) + len(turn.assistant_reply or "")
            for turn in self.recent_turns
        )
        if history_length > 4000:
            raise ValueError("最近对话上下文不得超过 4000 字")
        return self


class CaseContinuationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: Literal["same_case", "new_case"]
    scenario_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    facts: dict[str, Any] = Field(default_factory=dict)
    cleared_slots: list[str] = Field(default_factory=list)
    answer: str | None = Field(default=None, min_length=1, max_length=1200)
    action_refs: list[str] = Field(default_factory=list, max_length=3)
    citation_refs: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0, le=1)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    usage: UsageInfo = Field(default_factory=UsageInfo)

    @field_validator("cleared_slots", "action_refs", "citation_refs")
    @classmethod
    def reference_lists_are_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("引用和槽位名称不能为空")
        if len(values) != len(set(values)):
            raise ValueError("引用和槽位名称不得重复")
        return values

    @model_validator(mode="after")
    def route_fields_are_consistent(self) -> "CaseContinuationResult":
        overlap = set(self.facts).intersection(self.cleared_slots)
        if overlap:
            raise ValueError("同一槽位不能同时设置和撤回")
        if self.route == "same_case":
            if self.answer is None or not self.answer.strip():
                raise ValueError("同案续问必须提供短回答")
            return self
        if (
            self.answer is not None
            or self.facts
            or self.cleared_slots
            or self.action_refs
            or self.citation_refs
        ):
            raise ValueError("分案结果不得修改当前案件或携带回答引用")
        return self


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_topic_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]{1,99}$",
        validation_alias=AliasChoices(
            "candidate_topic_id",
            "scenario_id",
        ),
    )
    topic_label: str | None = Field(default=None, min_length=1, max_length=100)
    turn_intent: TurnIntent = "new_fact"
    facts: dict[str, Any] = Field(default_factory=dict)
    unknown_slots: list[str] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    explicit_question: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
    )
    bounded_answer: str | None = Field(
        default=None,
        min_length=1,
        max_length=1200,
    )
    facts_to_verify: list[str] = Field(
        default_factory=list,
        max_length=3,
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    usage: UsageInfo = Field(default_factory=UsageInfo)

    @property
    def scenario_id(self) -> str:
        """Compatibility accessor for callers using the former field name."""
        return self.candidate_topic_id

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_turn_intent(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "turn_intent" in value:
            return value
        inferred = dict(value)
        inferred["turn_intent"] = (
            "question"
            if inferred.get("explicit_question") is not None
            else "new_fact"
        )
        return inferred

    @model_validator(mode="after")
    def lists_are_unique(self) -> "ExtractionResult":
        if len(self.unknown_slots) != len(set(self.unknown_slots)):
            raise ValueError("unknown_slots 不得重复")
        if len(self.risk_flags) != len(set(self.risk_flags)):
            raise ValueError("risk_flags 不得重复")
        normalized_facts = [value.strip() for value in self.facts_to_verify]
        if any(not value for value in normalized_facts):
            raise ValueError("facts_to_verify 不得包含空项")
        if any(len(value) > 200 for value in normalized_facts):
            raise ValueError("facts_to_verify 单项长度不能超过 200")
        if len(normalized_facts) != len(set(normalized_facts)):
            raise ValueError("facts_to_verify 不得重复")
        if self.turn_intent == "question":
            if self.explicit_question is None or self.bounded_answer is None:
                raise ValueError("问题轮次必须包含问题摘录和直接回答")
        elif self.explicit_question is not None:
            raise ValueError("非问题轮次不得伪造明确问题")
        if self.turn_intent == "stated_goal" and self.bounded_answer is None:
            raise ValueError("陈述诉求时必须直接回应当前诉求")
        if self.bounded_answer is None and self.facts_to_verify:
            raise ValueError("没有回答时不得单独生成待核对事实")
        return self


class CoverageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: CoverageMode
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
    risk_flags: list[RiskFlag] = Field(default_factory=list)

    @model_validator(mode="after")
    def coverage_is_consistent(self) -> "CoverageResult":
        if self.mode == "formal":
            if self.playbook_id is None:
                raise ValueError("正式覆盖必须关联 Playbook")
        elif self.playbook_id is not None:
            raise ValueError("指导模式不得关联正式 Playbook")
        if self.mode == "emergency_guidance" and not self.risk_flags:
            raise ValueError("紧急指导必须包含风险标志")
        if len(self.risk_flags) != len(set(self.risk_flags)):
            raise ValueError("risk_flags 不得重复")
        return self


class RoutedExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage: CoverageResult
    facts: dict[str, Any] = Field(default_factory=dict)
    unknown_slots: list[str] = Field(default_factory=list)

    @field_validator("unknown_slots")
    @classmethod
    def routed_unknown_slots_are_unique(
        cls,
        values: list[str],
    ) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("unknown_slots 不得重复")
        return values


class CommunicationGuide(BaseModel):
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


class GuidanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direct_answer: str | None = Field(
        default=None,
        min_length=1,
        max_length=1200,
    )
    evidence_now: list[str] = Field(min_length=1, max_length=12)
    actions: list[str] = Field(min_length=1, max_length=12)
    communication_guide: CommunicationGuide
    limitations: list[str] = Field(min_length=1, max_length=12)
    next_question: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("evidence_now", "actions", "limitations")
    @classmethod
    def guidance_lists_are_clean(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("指导列表项不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("指导列表项不得重复")
        return normalized


class PolishingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=10000)
    purpose: Literal["summary", "communication", "action_step"]
    tone: Literal["plain", "firm", "polite"] = "plain"
