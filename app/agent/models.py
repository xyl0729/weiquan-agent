from __future__ import annotations

from typing import Any, Literal

from pydantic import (
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
        max_length=800,
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
    answer: str | None = Field(default=None, min_length=1, max_length=800)
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

    scenario_id: str = Field(min_length=1, max_length=100)
    facts: dict[str, Any] = Field(default_factory=dict)
    unknown_slots: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    usage: UsageInfo = Field(default_factory=UsageInfo)

    @model_validator(mode="after")
    def unknown_slots_are_unique(self) -> "ExtractionResult":
        if len(self.unknown_slots) != len(set(self.unknown_slots)):
            raise ValueError("unknown_slots 不得重复")
        return self


class PolishingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=10000)
    purpose: Literal["summary", "communication", "action_step"]
    tone: Literal["plain", "firm", "polite"] = "plain"
