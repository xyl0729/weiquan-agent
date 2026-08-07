from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class JurisdictionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[A-Z]{2}(?:-[A-Z0-9]{1,8})?$")
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list)
    small_claim_threshold_yuan: float | None = Field(default=None, gt=0)


class JurisdictionBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    default_code: str = Field(pattern=r"^[A-Z]{2}(?:-[A-Z0-9]{1,8})?$")
    jurisdictions: list[JurisdictionDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bundle(self) -> "JurisdictionBundle":
        codes = [item.code for item in self.jurisdictions]
        if len(codes) != len(set(codes)):
            raise ValueError("辖区 code 不得重复")
        if self.default_code not in set(codes):
            raise ValueError("default_code 未在 jurisdictions 中声明")

        aliases: dict[str, str] = {}
        for item in self.jurisdictions:
            for alias in [item.code, item.name, *item.aliases]:
                normalized = normalize_jurisdiction_alias(alias)
                existing = aliases.get(normalized)
                if existing is not None and existing != item.code:
                    raise ValueError(f"辖区别名冲突: {alias}")
                aliases[normalized] = item.code
        return self


class JurisdictionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str | None = None
    name: str | None = None
    status: Literal["supported", "unknown", "local_data_missing"]
    small_claim_threshold_yuan: float | None = Field(default=None, gt=0)
    messages: list[str] = Field(default_factory=list)


class TimeLimitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    status: Literal["active", "due_today", "expired", "unknown"]
    start_date: date | None = None
    deadline: date | None = None
    remaining_days: int | None = None
    legal_ref: str
    reminder: str


class JurisdictionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "escalate"]
    jurisdiction: JurisdictionResult
    time_limit: TimeLimitResult | None = None
    limitations: list[str] = Field(default_factory=list)


def normalize_jurisdiction_alias(value: str) -> str:
    return "".join(value.strip().casefold().split())
