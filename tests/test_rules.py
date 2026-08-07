from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.playbooks.evaluator import (
    evaluate_condition,
    evaluate_playbook,
    matching_escalations,
)
from app.playbooks.schema import Condition, Playbook


PROJECT_ROOT = Path(__file__).resolve().parent.parent

MIGRATED_SCENARIOS = (
    "prepaid_card",
    "overtime_pay",
    "return_refused",
    "counterfeit_goods",
    "training_refund",
    "auto_renewal",
    "renovation_default",
    "small_claim_procedure",
)


def load_playbook(scenario_id: str = "deposit_deduction") -> Playbook:
    path = (
        PROJECT_ROOT
        / "app"
        / "playbooks"
        / f"{scenario_id}.yaml"
    )
    return Playbook.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


@pytest.mark.parametrize(
    ("raw", "facts", "expected"),
    [
        ({"exists": "name"}, {"name": "value"}, True),
        ({"exists": "name"}, {}, False),
        ({"eq": {"slot": "name", "value": "a"}}, {"name": "a"}, True),
        ({"neq": {"slot": "name", "value": "a"}}, {"name": "b"}, True),
        ({"neq": {"slot": "name", "value": "a"}}, {}, False),
        (
            {"in": {"slot": "name", "values": ["a", "b"]}},
            {"name": "b"},
            True,
        ),
        ({"gte": {"slot": "amount", "value": 10}}, {"amount": 10}, True),
        ({"lte": {"slot": "amount", "value": 10}}, {"amount": 11}, False),
        (
            {"contains": {"slot": "name", "value": "租赁"}},
            {"name": "住房租赁合同"},
            True,
        ),
        (
            {
                "all": [
                    {"exists": "name"},
                    {"eq": {"slot": "amount", "value": 10}},
                ]
            },
            {"name": "x", "amount": 10},
            True,
        ),
        (
            {
                "any": [
                    {"eq": {"slot": "amount", "value": 9}},
                    {"eq": {"slot": "amount", "value": 10}},
                ]
            },
            {"amount": 10},
            True,
        ),
        (
            {"not": {"eq": {"slot": "name", "value": "a"}}},
            {"name": "b"},
            True,
        ),
    ],
)
def test_condition_operators(
    raw: dict[str, object],
    facts: dict[str, object],
    expected: bool,
) -> None:
    result = evaluate_condition(Condition.model_validate(raw), facts)

    assert result.matched is expected


def test_expression_like_string_is_only_compared_as_data() -> None:
    condition = Condition.model_validate(
        {
            "eq": {
                "slot": "name",
                "value": "__import__('os').system('whoami')",
            }
        }
    )

    result = evaluate_condition(
        condition,
        {"name": "ordinary user text"},
    )

    assert result.matched is False


def test_normal_wear_rule_is_selected_deterministically() -> None:
    result = evaluate_playbook(
        load_playbook(),
        {
            "deposit_amount": 2000,
            "withheld_amount": 2000,
            "landlord_reason": "normal_wear",
            "contract_has_deduction_term": False,
        },
    )

    assert result.verdict == "normal_wear_not_compensable"
    assert result.status == "ready"
    assert result.selected_rule_id == "normal_wear"
    assert result.matches[0].matched is True


def test_default_rule_remains_conservative() -> None:
    result = evaluate_playbook(
        load_playbook(),
        {
            "deposit_amount": 2000,
            "withheld_amount": 500,
            "landlord_reason": "minor_scuff",
            "contract_has_deduction_term": True,
        },
    )

    assert result.verdict == "need_more_facts"
    assert result.status == "need_more_facts"
    assert result.selected_rule_id == "default_conservative"


def test_rules_refuse_to_evaluate_with_missing_required_facts() -> None:
    with pytest.raises(ValueError, match="缺少必填槽位"):
        evaluate_playbook(
            load_playbook(),
            {"landlord_reason": "normal_wear"},
        )


def test_escalation_conditions_are_local_and_deterministic() -> None:
    messages = matching_escalations(
        load_playbook(),
        {
            "withheld_amount": 60000,
            "landlord_reason": "real_damage",
        },
    )

    assert len(messages) == 1
    assert "金额较高" in messages[0]


@pytest.mark.parametrize("scenario_id", MIGRATED_SCENARIOS)
def test_every_migrated_issue_rule_is_selected(
    scenario_id: str,
) -> None:
    playbook = load_playbook(scenario_id)
    issue_rules = [rule for rule in playbook.rules if not rule.default]
    assert issue_rules

    for rule in issue_rules:
        assert rule.when is not None
        assert rule.when.eq is not None
        assert rule.when.eq.slot == "issue_type"
        result = evaluate_playbook(
            playbook,
            {"issue_type": rule.when.eq.value},
        )
        assert result.selected_rule_id == rule.id
        assert result.verdict == rule.verdict
        assert result.status == "ready"


@pytest.mark.parametrize("scenario_id", MIGRATED_SCENARIOS)
def test_every_migrated_playbook_has_conservative_default(
    scenario_id: str,
) -> None:
    result = evaluate_playbook(
        load_playbook(scenario_id),
        {"issue_type": "other"},
    )

    assert result.selected_rule_id == "default_conservative"
    assert result.status == "need_more_facts"
