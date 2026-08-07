from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.playbooks.schema import (
    Condition,
    Playbook,
    condition_slot_names,
    validate_facts,
)


_MISSING = object()


class ConditionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    matched: bool
    inputs: list[str] = Field(default_factory=list)


class RuleMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    matched: bool
    inputs: list[str]
    verdict: str | None = None
    key_point: str | None = None


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: str
    verdict_label: str
    status: str
    selected_rule_id: str
    key_point: str
    facts: dict[str, Any]
    matches: list[RuleMatch]


def evaluate_condition(
    condition: Condition,
    facts: Mapping[str, Any],
) -> ConditionResult:
    inputs = condition_slot_names(condition)
    if condition.exists is not None:
        value = facts.get(condition.exists, _MISSING)
        return ConditionResult(
            matched=value is not _MISSING and value is not None,
            inputs=inputs,
        )
    if condition.eq is not None:
        return _binary(
            condition.eq.slot,
            condition.eq.value,
            facts,
            lambda left, right: left == right,
            inputs,
        )
    if condition.neq is not None:
        return _binary(
            condition.neq.slot,
            condition.neq.value,
            facts,
            lambda left, right: left != right,
            inputs,
        )
    if condition.in_ is not None:
        value = facts.get(condition.in_.slot, _MISSING)
        return ConditionResult(
            matched=(
                value is not _MISSING
                and value in condition.in_.values
            ),
            inputs=inputs,
        )
    if condition.gte is not None:
        return _ordered(
            condition.gte.slot,
            condition.gte.value,
            facts,
            greater=True,
            inputs=inputs,
        )
    if condition.lte is not None:
        return _ordered(
            condition.lte.slot,
            condition.lte.value,
            facts,
            greater=False,
            inputs=inputs,
        )
    if condition.contains is not None:
        value = facts.get(condition.contains.slot, _MISSING)
        expected = condition.contains.value
        matched = False
        if value is not _MISSING and value is not None:
            try:
                matched = expected in value
            except TypeError:
                matched = False
        return ConditionResult(matched=matched, inputs=inputs)
    if condition.all_ is not None:
        children = [
            evaluate_condition(child, facts)
            for child in condition.all_
        ]
        return ConditionResult(
            matched=all(child.matched for child in children),
            inputs=inputs,
        )
    if condition.any_ is not None:
        children = [
            evaluate_condition(child, facts)
            for child in condition.any_
        ]
        return ConditionResult(
            matched=any(child.matched for child in children),
            inputs=inputs,
        )
    if condition.not_ is not None:
        child = evaluate_condition(condition.not_, facts)
        return ConditionResult(
            matched=not child.matched,
            inputs=inputs,
        )
    raise ValueError("条件没有可执行操作符")


def evaluate_playbook(
    playbook: Playbook,
    facts: dict[str, Any],
) -> EvaluationResult:
    validated = validate_facts(playbook, facts, apply_defaults=True)
    if validated.missing_required:
        joined = ", ".join(validated.missing_required)
        raise ValueError(f"规则求值前仍缺少必填槽位: {joined}")

    matches: list[RuleMatch] = []
    selected = None
    for rule in playbook.rules:
        if rule.default:
            matched = selected is None
            result = ConditionResult(matched=matched, inputs=[])
        elif selected is None:
            result = evaluate_condition(rule.when, validated.facts)  # type: ignore[arg-type]
            matched = result.matched
        else:
            continue

        match = RuleMatch(
            rule_id=rule.id,
            matched=matched,
            inputs=result.inputs,
            verdict=rule.verdict if matched else None,
            key_point=rule.key_point if matched else None,
        )
        matches.append(match)
        if matched:
            selected = rule
            break

    if selected is None:
        raise ValueError("Playbook 没有可用的默认规则")
    verdict = playbook.verdict_by_code[selected.verdict]
    return EvaluationResult(
        verdict=verdict.code,
        verdict_label=verdict.label,
        status=verdict.status,
        selected_rule_id=selected.id,
        key_point=selected.key_point,
        facts=validated.facts,
        matches=matches,
    )


def matching_escalations(
    playbook: Playbook,
    facts: Mapping[str, Any],
) -> list[str]:
    return [
        item.message
        for item in playbook.escalation
        if evaluate_condition(item.when, facts).matched
    ]


def _binary(
    slot: str,
    expected: Any,
    facts: Mapping[str, Any],
    operation: Any,
    inputs: list[str],
) -> ConditionResult:
    value = facts.get(slot, _MISSING)
    return ConditionResult(
        matched=(
            value is not _MISSING
            and operation(value, expected)
        ),
        inputs=inputs,
    )


def _ordered(
    slot: str,
    expected: Any,
    facts: Mapping[str, Any],
    *,
    greater: bool,
    inputs: list[str],
) -> ConditionResult:
    value = facts.get(slot, _MISSING)
    if value is _MISSING:
        return ConditionResult(matched=False, inputs=inputs)
    try:
        matched = value >= expected if greater else value <= expected
    except TypeError:
        matched = False
    return ConditionResult(matched=matched, inputs=inputs)
