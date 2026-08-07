from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from app.jurisdiction.rules import (
    JurisdictionRegistry,
    calculate_time_limit,
    evaluate_jurisdiction,
)
from app.playbooks.schema import Playbook, TimeLimitDefinition


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def registry() -> JurisdictionRegistry:
    return JurisdictionRegistry.from_path(
        PROJECT_ROOT / "app" / "jurisdiction" / "data.yaml"
    )


def deposit_playbook() -> Playbook:
    path = PROJECT_ROOT / "app" / "playbooks" / "deposit_deduction.yaml"
    return Playbook.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def time_limit() -> TimeLimitDefinition:
    return TimeLimitDefinition(
        label="劳动仲裁申请时效",
        start_slot="dispute_date",
        amount=1,
        unit="years",
        legal_ref="劳动争议调解仲裁法.第二十七条",
        reminder="中断、中止和劳动关系存续期间的特别规则需另行核对。",
    )


def test_empty_and_alias_jurisdiction_use_registered_national_rules() -> None:
    for value in (None, "", "中国", " CN "):
        outcome = evaluate_jurisdiction(
            deposit_playbook(),
            registry(),
            jurisdiction=value,
            facts={},
            today=date(2026, 8, 6),
        )
        assert outcome.status == "ok"
        assert outcome.jurisdiction.code == "CN"
        assert outcome.jurisdiction.status == "supported"


def test_unknown_jurisdiction_escalates_without_guessing() -> None:
    outcome = evaluate_jurisdiction(
        deposit_playbook(),
        registry(),
        jurisdiction="未登记地区",
        facts={},
        today=date(2026, 8, 6),
    )

    assert outcome.status == "escalate"
    assert outcome.jurisdiction.status == "unknown"
    assert "不能推测" in outcome.limitations[0]


def test_required_local_threshold_escalates_when_not_registered() -> None:
    playbook = deposit_playbook().model_copy(
        update={"jurisdiction_mode": "required"}
    )

    outcome = evaluate_jurisdiction(
        playbook,
        registry(),
        jurisdiction="CN",
        facts={},
    )

    assert outcome.status == "escalate"
    assert outcome.jurisdiction.status == "local_data_missing"


def test_time_limit_handles_leap_year() -> None:
    result = calculate_time_limit(
        time_limit(),
        {"dispute_date": "2024-02-29"},
        today=date(2024, 3, 1),
    )

    assert result.status == "active"
    assert result.deadline == date(2025, 2, 28)
    assert result.remaining_days == 364


def test_time_limit_reports_due_today_and_expired() -> None:
    due = calculate_time_limit(
        time_limit(),
        {"dispute_date": "2025-08-06"},
        today=date(2026, 8, 6),
    )
    expired = calculate_time_limit(
        time_limit(),
        {"dispute_date": "2025-08-06"},
        today=date(2026, 8, 7),
    )

    assert due.status == "due_today"
    assert due.remaining_days == 0
    assert expired.status == "expired"
    assert expired.remaining_days == -1


def test_time_limit_is_unknown_without_valid_start_date() -> None:
    missing = calculate_time_limit(time_limit(), {}, today=date(2026, 8, 6))
    invalid = calculate_time_limit(
        time_limit(),
        {"dispute_date": "not-a-date"},
        today=date(2026, 8, 6),
    )

    assert missing.status == "unknown"
    assert invalid.status == "unknown"
    assert missing.deadline is None
