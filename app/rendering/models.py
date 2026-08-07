from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.jurisdiction.schema import (
    JurisdictionOutcome,
    JurisdictionResult,
    TimeLimitResult,
)
from app.playbooks.evaluator import EvaluationResult, RuleMatch
from app.playbooks.schema import Playbook
from app.retrieval.database import StatuteRecord


class LegalCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=3, max_length=200)
    law_name: str = Field(min_length=1, max_length=200)
    article_no: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)
    effective_date: date
    source_url: HttpUrl

    @classmethod
    def from_statute(cls, statute: StatuteRecord) -> "LegalCitation":
        if not isinstance(statute, StatuteRecord):
            raise TypeError("引用只能由 StatuteRecord 转换")
        return cls(
            ref=statute.ref,
            law_name=statute.law_name,
            article_no=statute.article_no,
            content=statute.content,
            effective_date=statute.effective_date,
            source_url=statute.source_url,
        )


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=1000)


class ActionStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=1000)


class VerdictView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    label: str = Field(min_length=1, max_length=200)
    status: Literal["need_more_facts", "ready", "escalate"]
    rule_ids: list[str] = Field(min_length=1)
    key_point: str = Field(min_length=1, max_length=500)


class ConsultationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    scenario_name: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1000)
    evidence_now: list[EvidenceItem] = Field(min_length=1)
    time_limit: TimeLimitResult | None = None
    verdict: VerdictView
    citations: list[LegalCitation] = Field(min_length=1)
    actions: list[ActionStep] = Field(min_length=1)
    communication_text: str = Field(min_length=1, max_length=2000)
    limitations: list[str] = Field(min_length=1)
    jurisdiction: JurisdictionResult


class LockedConsultationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    playbook_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    facts: dict[str, Any]
    rule_matches: list[RuleMatch]
    plan: ConsultationPlan


class RenderedDocuments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_text: str = Field(min_length=1)
    evidence_request_text: str = Field(min_length=1)
    polish_applied: bool = False


def build_consultation_draft(
    playbook: Playbook,
    evaluation: EvaluationResult,
    statutes: Sequence[StatuteRecord],
    jurisdiction: JurisdictionOutcome,
    *,
    additional_limitations: Sequence[str] = (),
) -> LockedConsultationDraft:
    citations = _citations(playbook, statutes)
    verdict = VerdictView(
        code=evaluation.verdict,
        label=evaluation.verdict_label,
        status=evaluation.status,
        rule_ids=[evaluation.selected_rule_id],
        key_point=evaluation.key_point,
    )
    limitations = _unique_text(
        [
            *playbook.limitations,
            *jurisdiction.limitations,
            *additional_limitations,
        ]
    )
    plan = ConsultationPlan(
        scenario_id=playbook.id,
        scenario_name=playbook.name,
        summary=(
            f"依据当前已确认事实，规则结果为“{evaluation.verdict_label}”。"
            f"{evaluation.key_point}"
        ),
        evidence_now=[
            EvidenceItem(order=index, text=text)
            for index, text in enumerate(playbook.evidence_now, start=1)
        ],
        time_limit=jurisdiction.time_limit,
        verdict=verdict,
        citations=citations,
        actions=[
            ActionStep(order=index, text=text)
            for index, text in enumerate(playbook.actions, start=1)
        ],
        communication_text=(
            "沟通时请逐项列明争议事实、金额和对应材料，"
            "要求对方书面回复，并保留发送和送达记录。"
        ),
        limitations=limitations,
        jurisdiction=jurisdiction.jurisdiction,
    )
    return LockedConsultationDraft(
        scenario_id=playbook.id,
        playbook_version=playbook.version,
        facts=evaluation.facts,
        rule_matches=evaluation.matches,
        plan=plan,
    )


def _citations(
    playbook: Playbook,
    statutes: Sequence[StatuteRecord],
) -> list[LegalCitation]:
    by_ref: dict[str, StatuteRecord] = {}
    for statute in statutes:
        if not isinstance(statute, StatuteRecord):
            raise TypeError("引用只能由 StatuteRecord 转换")
        by_ref.setdefault(statute.ref, statute)

    required_refs = [basis.ref for basis in playbook.legal_basis]
    missing = [ref for ref in required_refs if ref not in by_ref]
    if missing:
        raise ValueError("方案缺少强制法条引用: " + ", ".join(missing))

    return [
        LegalCitation.from_statute(by_ref[ref])
        for ref in required_refs
    ]


def _unique_text(values: Sequence[str]) -> list[str]:
    normalized = [value.strip() for value in values if value.strip()]
    return list(dict.fromkeys(normalized))
