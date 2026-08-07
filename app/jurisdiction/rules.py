from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from dateutil.relativedelta import relativedelta

from app.jurisdiction.schema import (
    JurisdictionBundle,
    JurisdictionDefinition,
    JurisdictionOutcome,
    JurisdictionResult,
    TimeLimitResult,
    normalize_jurisdiction_alias,
)
from app.playbooks.schema import Playbook, TimeLimitDefinition


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class JurisdictionRegistry:
    def __init__(self, bundle: JurisdictionBundle) -> None:
        self.bundle = bundle
        self._by_code = {
            item.code: item
            for item in bundle.jurisdictions
        }
        self._aliases: dict[str, str] = {}
        for item in bundle.jurisdictions:
            for alias in [item.code, item.name, *item.aliases]:
                self._aliases[normalize_jurisdiction_alias(alias)] = item.code

    @classmethod
    def from_path(cls, path: Path) -> "JurisdictionRegistry":
        resolved = path.resolve()
        if not resolved.is_relative_to(PROJECT_ROOT):
            raise ValueError("辖区配置必须位于项目内")
        if not resolved.is_file():
            raise FileNotFoundError(f"辖区配置不存在: {resolved}")
        with resolved.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        if not isinstance(raw, dict):
            raise ValueError("辖区配置根节点必须是对象")
        return cls(JurisdictionBundle.model_validate(raw))

    def resolve(
        self,
        value: str | None,
    ) -> JurisdictionDefinition | None:
        normalized = normalize_jurisdiction_alias(value or "")
        if not normalized:
            return self._by_code[self.bundle.default_code]
        code = self._aliases.get(normalized)
        return self._by_code.get(code) if code else None


def evaluate_jurisdiction(
    playbook: Playbook,
    registry: JurisdictionRegistry,
    *,
    jurisdiction: str | None,
    facts: dict[str, Any],
    today: date | None = None,
) -> JurisdictionOutcome:
    resolved = registry.resolve(jurisdiction)
    if resolved is None:
        requested = (jurisdiction or "").strip()
        message = (
            f"辖区“{requested}”未在本地核验配置中登记，"
            "不能推测当地标准。"
        )
        return JurisdictionOutcome(
            status="escalate",
            jurisdiction=JurisdictionResult(
                status="unknown",
                messages=[message],
            ),
            limitations=[message],
        )

    messages = ["仅应用项目内已登记的全国性规则。"]
    jurisdiction_status = "supported"
    outcome_status = "ok"
    limitations: list[str] = []
    if (
        playbook.jurisdiction_mode == "required"
        and resolved.small_claim_threshold_yuan is None
    ):
        jurisdiction_status = "local_data_missing"
        outcome_status = "escalate"
        message = (
            "该场景需要当地金额或程序标准，但本地核验配置尚未登记，"
            "请向受理法院或当地专业人员核实。"
        )
        messages.append(message)
        limitations.append(message)

    time_limit = (
        calculate_time_limit(
            playbook.time_limit,
            facts,
            today=today,
        )
        if playbook.time_limit is not None
        else None
    )
    return JurisdictionOutcome(
        status=outcome_status,
        jurisdiction=JurisdictionResult(
            code=resolved.code,
            name=resolved.name,
            status=jurisdiction_status,
            small_claim_threshold_yuan=(
                resolved.small_claim_threshold_yuan
            ),
            messages=messages,
        ),
        time_limit=time_limit,
        limitations=limitations,
    )


def calculate_time_limit(
    specification: TimeLimitDefinition,
    facts: dict[str, Any],
    *,
    today: date | None = None,
) -> TimeLimitResult:
    raw_start = facts.get(specification.start_slot)
    if raw_start is None:
        return TimeLimitResult(
            label=specification.label,
            status="unknown",
            legal_ref=specification.legal_ref,
            reminder=specification.reminder,
        )
    try:
        start = (
            raw_start
            if isinstance(raw_start, date)
            else date.fromisoformat(str(raw_start))
        )
    except (TypeError, ValueError):
        return TimeLimitResult(
            label=specification.label,
            status="unknown",
            legal_ref=specification.legal_ref,
            reminder=specification.reminder,
        )

    if specification.unit == "days":
        deadline = start + relativedelta(days=specification.amount)
    elif specification.unit == "months":
        deadline = start + relativedelta(months=specification.amount)
    else:
        deadline = start + relativedelta(years=specification.amount)

    current = today or date.today()
    remaining_days = (deadline - current).days
    if remaining_days < 0:
        status = "expired"
    elif remaining_days == 0:
        status = "due_today"
    else:
        status = "active"
    return TimeLimitResult(
        label=specification.label,
        status=status,
        start_date=start,
        deadline=deadline,
        remaining_days=remaining_days,
        legal_ref=specification.legal_ref,
        reminder=specification.reminder,
    )
