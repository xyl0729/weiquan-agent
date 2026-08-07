from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.playbooks.registry import PlaybookRegistry
from app.playbooks.schema import Playbook, validate_facts


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_PATH = PROJECT_ROOT / "app" / "playbooks" / "deposit_deduction.yaml"


def load_document() -> dict[str, object]:
    document = yaml.safe_load(PLAYBOOK_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_registry_loads_formal_playbook_and_skips_development_demo() -> None:
    registry = PlaybookRegistry.from_directory(
        PROJECT_ROOT / "app" / "playbooks"
    )

    assert registry.scenario_ids == [
        "auto_renewal",
        "counterfeit_goods",
        "deposit_deduction",
        "overtime_pay",
        "prepaid_card",
        "renovation_default",
        "return_refused",
        "small_claim_procedure",
        "training_refund",
    ]
    assert registry.resolve(" 房东 扣押金 ") is not None
    assert registry.resolve("候选数据测试") is None
    context = registry.provider_context(scenario_id="deposit_deduction")
    assert "contract_has_deduction_term" in context["allowed_slot_names"]


def test_provider_context_keeps_shared_slots_scoped_by_scenario() -> None:
    registry = PlaybookRegistry.from_directory(
        PROJECT_ROOT / "app" / "playbooks"
    )

    context = registry.provider_context()
    definitions = context["scenario_definitions"]
    assert isinstance(definitions, dict)
    assert set(definitions) == set(registry.scenario_ids)

    prepaid = definitions["prepaid_card"]
    overtime = definitions["overtime_pay"]
    assert isinstance(prepaid, dict)
    assert isinstance(overtime, dict)
    prepaid_slots = prepaid["slot_definitions"]
    overtime_slots = overtime["slot_definitions"]
    assert isinstance(prepaid_slots, dict)
    assert isinstance(overtime_slots, dict)
    assert "closure" in prepaid_slots["issue_type"]["values"]
    assert (
        "overtime_compensation"
        in overtime_slots["issue_type"]["values"]
    )
    assert (
        prepaid_slots["issue_type"]["values"]
        != overtime_slots["issue_type"]["values"]
    )
    assert prepaid["required_slot_names"] == ["issue_type"]
    assert overtime["required_slot_names"] == ["issue_type"]


def test_formal_playbook_references_resolve_to_verified_database() -> None:
    registry = PlaybookRegistry.from_directory(
        PROJECT_ROOT / "app" / "playbooks"
    )

    registry.verify_references(PROJECT_ROOT / "data" / "statutes.db")


def test_optional_slot_must_explicitly_declare_default() -> None:
    document = load_document()
    optional = document["slots"]["optional"]  # type: ignore[index]
    del optional[0]["default"]

    with pytest.raises(ValidationError, match="显式声明 default"):
        Playbook.model_validate(document)


def test_rule_may_not_reference_undeclared_slot() -> None:
    document = load_document()
    rules = document["rules"]  # type: ignore[index]
    rules[0]["when"]["eq"]["slot"] = "hidden_slot"

    with pytest.raises(ValidationError, match="未声明槽位"):
        Playbook.model_validate(document)


def test_unknown_verdict_is_rejected() -> None:
    document = load_document()
    rules = document["rules"]  # type: ignore[index]
    rules[0]["verdict"] = "tenant_always_wins"

    with pytest.raises(ValidationError, match="未知 verdict"):
        Playbook.model_validate(document)


def test_default_rule_cannot_produce_ready_result() -> None:
    document = load_document()
    rules = document["rules"]  # type: ignore[index]
    rules[-1]["verdict"] = "deduction_lacks_stated_basis"

    with pytest.raises(ValidationError, match="默认规则只能"):
        Playbook.model_validate(document)


def test_registry_rejects_duplicate_id_and_alias() -> None:
    playbook = Playbook.model_validate(load_document())

    with pytest.raises(ValueError, match="ID 重复"):
        PlaybookRegistry([playbook, playbook])

    second_document = deepcopy(load_document())
    second_document["id"] = "second_deposit"
    second_document["aliases"] = ["押金"]
    second = Playbook.model_validate(second_document)
    with pytest.raises(ValueError, match="别名冲突"):
        PlaybookRegistry([playbook, second])


def test_fact_validation_normalizes_types_defaults_and_missing() -> None:
    playbook = Playbook.model_validate(load_document())

    result = validate_facts(
        playbook,
        {
            "deposit_amount": 2000,
            "landlord_reason": "normal_wear",
            "contract_has_deduction_term": False,
        },
    )

    assert result.facts["deposit_amount"] == 2000.0
    assert result.facts["has_checkout_photos"] is False
    assert result.missing_required == ["withheld_amount"]


@pytest.mark.parametrize(
    ("facts", "error"),
    [
        ({"unknown_slot": "x"}, "未声明槽位"),
        ({"deposit_amount": -1}, "不能小于"),
        ({"deposit_amount": True}, "有限数值"),
        ({"landlord_reason": "invented_reason"}, "允许枚举"),
        ({"lease_end_date": "2026-02-30"}, "ISO 日期"),
    ],
)
def test_fact_validation_rejects_invalid_values(
    facts: dict[str, object],
    error: str,
) -> None:
    playbook = Playbook.model_validate(load_document())

    with pytest.raises(ValueError, match=error):
        validate_facts(playbook, facts)
