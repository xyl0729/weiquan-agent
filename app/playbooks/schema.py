from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
_VERSION_PATTERN = r"^\d+\.\d+\.\d+$"
_MISSING = object()

SlotType = Literal["string", "number", "integer", "boolean", "date", "enum"]
ResultStatus = Literal["need_more_facts", "ready", "escalate"]
JurisdictionMode = Literal["national", "required"]


class SlotDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_IDENTIFIER_PATTERN)
    type: SlotType
    ask: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    values: list[str] | None = Field(default=None, min_length=1)
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = Field(default=None, ge=1, le=10000)
    default: Any = None

    @model_validator(mode="after")
    def validate_type_constraints(self) -> "SlotDefinition":
        if self.type == "enum":
            if not self.values:
                raise ValueError("enum 槽位必须声明 values")
            if len(self.values) != len(set(self.values)):
                raise ValueError("enum values 不得重复")
        elif self.values is not None:
            raise ValueError("只有 enum 槽位可以声明 values")

        if self.type not in {"number", "integer"} and (
            self.minimum is not None or self.maximum is not None
        ):
            raise ValueError("只有数值槽位可以声明 minimum/maximum")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum 不能大于 maximum")
        if self.type not in {"string", "enum"} and self.max_length is not None:
            raise ValueError("只有字符串或枚举槽位可以声明 max_length")
        return self


class SlotCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[SlotDefinition] = Field(default_factory=list)
    optional: list[SlotDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_slots(self) -> "SlotCollection":
        all_slots = [*self.required, *self.optional]
        names = [slot.name for slot in all_slots]
        if len(names) != len(set(names)):
            raise ValueError("槽位名称不得重复")

        for slot in self.required:
            if not slot.ask:
                raise ValueError(f"必填槽位 {slot.name} 必须声明 ask")
        for slot in self.optional:
            if "default" not in slot.model_fields_set:
                raise ValueError(f"可选槽位 {slot.name} 必须显式声明 default")
            if slot.default is not None:
                normalize_slot_value(slot, slot.default)
        return self

    @property
    def all(self) -> list[SlotDefinition]:
        return [*self.required, *self.optional]

    @property
    def by_name(self) -> dict[str, SlotDefinition]:
        return {slot.name: slot for slot in self.all}


class BinaryPredicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: str = Field(pattern=_IDENTIFIER_PATTERN)
    value: Any

    @field_validator("value")
    @classmethod
    def value_is_scalar(cls, value: Any) -> Any:
        if not _is_scalar(value):
            raise ValueError("比较值必须是标量")
        return value


class MembershipPredicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: str = Field(pattern=_IDENTIFIER_PATTERN)
    values: list[Any] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def values_are_scalars(cls, values: list[Any]) -> list[Any]:
        if any(not _is_scalar(value) for value in values):
            raise ValueError("in.values 只能包含标量")
        return values


class Condition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    exists: str | None = Field(default=None, pattern=_IDENTIFIER_PATTERN)
    eq: BinaryPredicate | None = None
    neq: BinaryPredicate | None = None
    in_: MembershipPredicate | None = Field(default=None, alias="in")
    gte: BinaryPredicate | None = None
    lte: BinaryPredicate | None = None
    contains: BinaryPredicate | None = None
    all_: list["Condition"] | None = Field(
        default=None,
        alias="all",
        min_length=1,
    )
    any_: list["Condition"] | None = Field(
        default=None,
        alias="any",
        min_length=1,
    )
    not_: "Condition | None" = Field(default=None, alias="not")

    @model_validator(mode="after")
    def exactly_one_operator(self) -> "Condition":
        values = (
            self.exists,
            self.eq,
            self.neq,
            self.in_,
            self.gte,
            self.lte,
            self.contains,
            self.all_,
            self.any_,
            self.not_,
        )
        if sum(value is not None for value in values) != 1:
            raise ValueError("条件节点必须且只能包含一个操作符")
        return self


class VerdictDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=_IDENTIFIER_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    status: ResultStatus


class LegalBasisDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=3, max_length=200)
    purpose: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("ref")
    @classmethod
    def ref_has_one_separator(cls, value: str) -> str:
        normalized = value.strip()
        law, separator, article = normalized.partition(".")
        if not separator or not law or not article or "." in article:
            raise ValueError("法条引用必须使用 法律简称.条号 格式")
        return normalized


class RuleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_IDENTIFIER_PATTERN)
    when: Condition | None = None
    default: bool = False
    verdict: str = Field(pattern=_IDENTIFIER_PATTERN)
    key_point: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_branch(self) -> "RuleDefinition":
        if self.default and self.when is not None:
            raise ValueError("默认规则不能声明 when")
        if not self.default and self.when is None:
            raise ValueError("非默认规则必须声明 when")
        return self


class EscalationDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_IDENTIFIER_PATTERN)
    when: Condition
    message: str = Field(min_length=1, max_length=500)


class TemplateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: Literal["consultation_plan.j2"] = "consultation_plan.j2"
    evidence: Literal["evidence_request.j2"] = "evidence_request.j2"


class TimeLimitDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=200)
    start_slot: str = Field(pattern=_IDENTIFIER_PATTERN)
    amount: int = Field(gt=0, le=100)
    unit: Literal["days", "months", "years"]
    legal_ref: str = Field(min_length=3, max_length=200)
    reminder: str = Field(min_length=1, max_length=500)


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=_VERSION_PATTERN)
    id: str = Field(pattern=_IDENTIFIER_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    development_only: bool = False
    aliases: list[str] = Field(default_factory=list)
    jurisdiction_mode: JurisdictionMode = "national"
    slots: SlotCollection
    verdicts: list[VerdictDefinition] = Field(min_length=1)
    legal_basis: list[LegalBasisDefinition] = Field(min_length=1)
    rules: list[RuleDefinition] = Field(min_length=1)
    evidence_now: list[str] = Field(min_length=1)
    actions: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    escalation: list[EscalationDefinition] = Field(default_factory=list)
    templates: TemplateDefinition = Field(default_factory=TemplateDefinition)
    time_limit: TimeLimitDefinition | None = None

    @field_validator(
        "aliases",
        "evidence_now",
        "actions",
        "limitations",
    )
    @classmethod
    def non_empty_unique_text(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("文本列表不能包含空值")
        if len(normalized) != len(set(normalized)):
            raise ValueError("文本列表不能包含重复值")
        return normalized

    @model_validator(mode="after")
    def validate_contract(self) -> "Playbook":
        if self.development_only:
            raise ValueError("正式 Playbook 不能标记 development_only")

        verdict_codes = [verdict.code for verdict in self.verdicts]
        if len(verdict_codes) != len(set(verdict_codes)):
            raise ValueError("verdict code 不得重复")

        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rule id 不得重复")
        if sum(rule.default for rule in self.rules) != 1:
            raise ValueError("必须且只能有一个默认规则")
        if not self.rules[-1].default:
            raise ValueError("默认规则必须位于规则列表末尾")

        allowed_verdicts = set(verdict_codes)
        unknown_verdicts = {
            rule.verdict
            for rule in self.rules
            if rule.verdict not in allowed_verdicts
        }
        if unknown_verdicts:
            raise ValueError(
                f"规则引用未知 verdict: {', '.join(sorted(unknown_verdicts))}"
            )

        verdict_by_code = {
            verdict.code: verdict
            for verdict in self.verdicts
        }
        default_rule = self.rules[-1]
        if verdict_by_code[default_rule.verdict].status == "ready":
            raise ValueError("默认规则只能返回保守追问或升级结果")

        slot_by_name = self.slots.by_name
        for rule in self.rules:
            if rule.when is not None:
                _validate_condition(rule.when, slot_by_name)
        escalation_ids = [item.id for item in self.escalation]
        if len(escalation_ids) != len(set(escalation_ids)):
            raise ValueError("escalation id 不得重复")
        for item in self.escalation:
            _validate_condition(item.when, slot_by_name)

        refs = [item.ref for item in self.legal_basis]
        if len(refs) != len(set(refs)):
            raise ValueError("legal_basis 引用不得重复")
        if self.time_limit is not None:
            if self.time_limit.start_slot not in slot_by_name:
                raise ValueError("时效起算槽位未声明")
            if slot_by_name[self.time_limit.start_slot].type != "date":
                raise ValueError("时效起算槽位必须是 date")
            if self.time_limit.legal_ref not in set(refs):
                raise ValueError("时效 legal_ref 必须在 legal_basis 中声明")
        return self

    @property
    def verdict_by_code(self) -> dict[str, VerdictDefinition]:
        return {verdict.code: verdict for verdict in self.verdicts}

    @property
    def required_slot_names(self) -> list[str]:
        return [slot.name for slot in self.slots.required]

    @property
    def slot_names(self) -> list[str]:
        return [slot.name for slot in self.slots.all]


class SlotValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: dict[str, Any]
    missing_required: list[str]


def validate_facts(
    playbook: Playbook,
    facts: dict[str, Any],
    *,
    apply_defaults: bool = True,
) -> SlotValidationResult:
    slot_by_name = playbook.slots.by_name
    unknown = set(facts) - set(slot_by_name)
    if unknown:
        raise ValueError(f"存在未声明槽位: {', '.join(sorted(unknown))}")

    normalized: dict[str, Any] = {}
    for name, value in facts.items():
        if value is None:
            continue
        normalized[name] = normalize_slot_value(slot_by_name[name], value)

    if apply_defaults:
        for slot in playbook.slots.optional:
            if slot.name not in normalized and slot.default is not None:
                normalized[slot.name] = normalize_slot_value(
                    slot,
                    slot.default,
                )

    missing = [
        slot.name
        for slot in playbook.slots.required
        if slot.name not in normalized
    ]
    return SlotValidationResult(
        facts=normalized,
        missing_required=missing,
    )


def normalize_slot_value(slot: SlotDefinition, value: Any) -> Any:
    if slot.type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{slot.name} 必须是字符串")
        normalized: Any = value.strip()
        if not normalized:
            raise ValueError(f"{slot.name} 不能为空")
    elif slot.type == "enum":
        if not isinstance(value, str):
            raise ValueError(f"{slot.name} 必须是枚举字符串")
        normalized = value.strip()
        if normalized not in set(slot.values or []):
            raise ValueError(f"{slot.name} 不在允许枚举中")
    elif slot.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{slot.name} 必须是布尔值")
        normalized = value
    elif slot.type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{slot.name} 必须是整数")
        normalized = value
    elif slot.type == "number":
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"{slot.name} 必须是有限数值")
        normalized = float(value)
    elif slot.type == "date":
        if isinstance(value, date):
            normalized = value.isoformat()
        elif isinstance(value, str):
            try:
                normalized = date.fromisoformat(value.strip()).isoformat()
            except ValueError as exc:
                raise ValueError(f"{slot.name} 必须是 ISO 日期") from exc
        else:
            raise ValueError(f"{slot.name} 必须是 ISO 日期")
    else:
        raise ValueError(f"未知槽位类型: {slot.type}")

    if isinstance(normalized, str):
        limit = slot.max_length or 2000
        if len(normalized) > limit:
            raise ValueError(f"{slot.name} 长度不能超过 {limit}")
    if slot.type in {"number", "integer"}:
        numeric = float(normalized)
        if slot.minimum is not None and numeric < slot.minimum:
            raise ValueError(f"{slot.name} 不能小于 {slot.minimum}")
        if slot.maximum is not None and numeric > slot.maximum:
            raise ValueError(f"{slot.name} 不能大于 {slot.maximum}")
    return normalized


def condition_slot_names(condition: Condition) -> list[str]:
    if condition.exists is not None:
        return [condition.exists]
    for predicate in (
        condition.eq,
        condition.neq,
        condition.in_,
        condition.gte,
        condition.lte,
        condition.contains,
    ):
        if predicate is not None:
            return [predicate.slot]
    if condition.all_ is not None:
        return _dedupe(
            name
            for child in condition.all_
            for name in condition_slot_names(child)
        )
    if condition.any_ is not None:
        return _dedupe(
            name
            for child in condition.any_
            for name in condition_slot_names(child)
        )
    if condition.not_ is not None:
        return condition_slot_names(condition.not_)
    raise ValueError("条件没有操作符")


def _validate_condition(
    condition: Condition,
    slot_by_name: dict[str, SlotDefinition],
) -> None:
    unknown = set(condition_slot_names(condition)) - set(slot_by_name)
    if unknown:
        raise ValueError(f"条件引用未声明槽位: {', '.join(sorted(unknown))}")

    for operation, predicate in (
        ("eq", condition.eq),
        ("neq", condition.neq),
        ("gte", condition.gte),
        ("lte", condition.lte),
        ("contains", condition.contains),
    ):
        if predicate is None:
            continue
        slot = slot_by_name[predicate.slot]
        if operation in {"gte", "lte"} and slot.type not in {
            "number",
            "integer",
            "date",
        }:
            raise ValueError(f"{operation} 仅支持数值或日期槽位")
        if operation == "contains" and slot.type not in {"string", "enum"}:
            raise ValueError("contains 仅支持字符串或枚举槽位")
        normalize_slot_value(slot, predicate.value)

    if condition.in_ is not None:
        slot = slot_by_name[condition.in_.slot]
        for value in condition.in_.values:
            normalize_slot_value(slot, value)
    for children in (condition.all_, condition.any_):
        if children is not None:
            for child in children:
                _validate_condition(child, slot_by_name)
    if condition.not_ is not None:
        _validate_condition(condition.not_, slot_by_name)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool, date))


def _dedupe(values: Any) -> list[str]:
    return list(dict.fromkeys(values))
