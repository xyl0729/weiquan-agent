from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
