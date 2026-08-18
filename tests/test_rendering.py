from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from app.agent.errors import ProviderError
from app.jurisdiction.rules import JurisdictionRegistry, evaluate_jurisdiction
from app.playbooks.evaluator import evaluate_playbook
from app.playbooks.registry import PlaybookRegistry
from app.playbooks.schema import Playbook
from app.rendering.communication import build_communication_guide
from app.rendering.models import LegalCitation, build_consultation_draft
from app.rendering.renderer import PlanRenderer
from app.retrieval.database import (
    StatuteRecord,
    connect_database,
    get_statute_by_ref,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _playbook() -> Playbook:
    path = PROJECT_ROOT / "app" / "playbooks" / "deposit_deduction.yaml"
    return Playbook.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def _statutes(playbook: Playbook) -> list[StatuteRecord]:
    connection = connect_database(PROJECT_ROOT / "data" / "statutes.db")
    try:
        records = [
            get_statute_by_ref(connection, basis.ref)
            for basis in playbook.legal_basis
        ]
    finally:
        connection.close()
    assert all(record is not None for record in records)
    return [record for record in records if record is not None]


def _draft():
    playbook = _playbook()
    evaluation = evaluate_playbook(
        playbook,
        {
            "deposit_amount": 2000,
            "withheld_amount": 2000,
            "landlord_reason": "normal_wear",
            "contract_has_deduction_term": False,
        },
    )
    jurisdictions = JurisdictionRegistry.from_path(
        PROJECT_ROOT / "app" / "jurisdiction" / "data.yaml"
    )
    outcome = evaluate_jurisdiction(
        playbook,
        jurisdictions,
        jurisdiction="CN",
        facts=evaluation.facts,
    )
    return build_consultation_draft(
        playbook,
        evaluation,
        _statutes(playbook),
        outcome,
    )


def _required_facts(playbook: Playbook) -> dict[str, object]:
    facts: dict[str, object] = {}
    for slot in playbook.slots.required:
        if slot.type == "enum":
            facts[slot.name] = (slot.values or [])[0]
        elif slot.type == "number":
            facts[slot.name] = 1000
        elif slot.type == "integer":
            facts[slot.name] = 1
        elif slot.type == "boolean":
            facts[slot.name] = False
        elif slot.type == "date":
            facts[slot.name] = "2026-08-08"
        else:
            facts[slot.name] = "已确认事实"
    return facts


def test_citation_can_only_be_converted_from_statute_record() -> None:
    with pytest.raises(TypeError, match="StatuteRecord"):
        LegalCitation.from_statute(  # type: ignore[arg-type]
            {
                "ref": "伪造法.第一条",
                "content": "模型生成内容",
            }
        )


def test_builder_requires_every_mandatory_reference() -> None:
    playbook = _playbook()
    draft = _draft()
    evaluation = evaluate_playbook(playbook, draft.facts)
    jurisdictions = JurisdictionRegistry.from_path(
        PROJECT_ROOT / "app" / "jurisdiction" / "data.yaml"
    )
    outcome = evaluate_jurisdiction(
        playbook,
        jurisdictions,
        jurisdiction="CN",
        facts=evaluation.facts,
    )

    with pytest.raises(ValueError, match="缺少强制法条引用"):
        build_consultation_draft(
            playbook,
            evaluation,
            _statutes(playbook)[:-1],
            outcome,
        )


def test_builder_ignores_statutes_outside_playbook_legal_basis() -> None:
    playbook = _playbook()
    evaluation = evaluate_playbook(playbook, _draft().facts)
    jurisdictions = JurisdictionRegistry.from_path(
        PROJECT_ROOT / "app" / "jurisdiction" / "data.yaml"
    )
    outcome = evaluate_jurisdiction(
        playbook,
        jurisdictions,
        jurisdiction="CN",
        facts=evaluation.facts,
    )
    connection = connect_database(PROJECT_ROOT / "data" / "statutes.db")
    try:
        unrelated = get_statute_by_ref(
            connection,
            "消费者权益保护法.第二十四条",
        )
    finally:
        connection.close()
    assert unrelated is not None

    draft = build_consultation_draft(
        playbook,
        evaluation,
        [*_statutes(playbook), unrelated],
        outcome,
    )

    assert [citation.ref for citation in draft.plan.citations] == [
        basis.ref for basis in playbook.legal_basis
    ]


def test_all_formal_playbooks_build_complete_communication_guides() -> None:
    registry = PlaybookRegistry.from_directory(
        PROJECT_ROOT / "app" / "playbooks"
    )

    for playbook in registry.playbooks:
        evaluation = evaluate_playbook(
            playbook,
            _required_facts(playbook),
        )

        guide = build_communication_guide(playbook, evaluation)

        assert guide == build_communication_guide(playbook, evaluation)
        assert guide.recipient != "对方"
        assert guide.channels
        assert guide.when_to_send
        assert guide.objective
        assert playbook.name in guide.message
        assert evaluation.verdict_label in guide.message
        assert guide.after_sending
        assert guide.escalation
        assert guide.required_before_send == []


def test_draft_keeps_legacy_communication_text_in_sync() -> None:
    draft = _draft()

    assert (
        draft.plan.communication_text
        == draft.plan.communication_guide.message
    )


def test_fixed_templates_render_evidence_first_and_escape_input() -> None:
    draft = _draft()
    injected_guide = draft.plan.communication_guide.model_copy(
        update={"message": "<script>alert(1)</script>"}
    )
    injected = draft.plan.model_copy(
        update={
            "communication_text": "<script>alert(1)</script>",
            "communication_guide": injected_guide,
        }
    )
    draft = draft.model_copy(update={"plan": injected})
    renderer = PlanRenderer(
        PROJECT_ROOT / "app" / "rendering" / "templates"
    )

    documents = renderer.render(draft)

    assert documents.plan_text.startswith("【立即保全证据】")
    assert documents.plan_text.index("【立即保全证据】") < (
        documents.plan_text.index("【初步判断】")
    )
    assert "<script>" not in documents.plan_text
    assert "&lt;script&gt;" in documents.plan_text
    assert "住房租赁条例第十条" in documents.plan_text
    assert "【沟通指南】" in documents.plan_text
    assert "收件对象：" in documents.plan_text
    assert "建议渠道：" in documents.plan_text
    assert "发送时机：" in documents.plan_text
    assert "【发送后动作】" in documents.plan_text
    assert "【升级动作】" in documents.plan_text
    assert documents.evidence_request_text.startswith(
        "主题：关于租房押金扣减"
    )


def test_renderer_rejects_non_whitelisted_template_name() -> None:
    renderer = PlanRenderer(
        PROJECT_ROOT / "app" / "rendering" / "templates"
    )

    with pytest.raises(ValueError, match="白名单"):
        renderer.render(_draft(), plan_template="../../secret.j2")


def test_polishing_failure_falls_back_to_locked_template_text() -> None:
    renderer = PlanRenderer(
        PROJECT_ROOT / "app" / "rendering" / "templates"
    )
    original = renderer.render(_draft())

    class FailingProvider:
        name = "failing"
        model = "failing"

        async def extract_facts(self, message, context):  # pragma: no cover
            raise AssertionError

        async def polish_text(self, draft):
            raise ProviderError("provider_timeout")

    rendered = asyncio.run(
        renderer.render_with_optional_polish(
            _draft(),
            provider=FailingProvider(),
        )
    )

    assert rendered.polish_applied is False
    assert rendered.plan_text == original.plan_text


def test_polishing_updates_both_communication_message_fields_only() -> None:
    class SuccessfulProvider:
        name = "successful"
        model = "successful"

        async def extract_facts(self, message, context):  # pragma: no cover
            raise AssertionError

        async def polish_text(self, draft):
            return "这是润色后且可直接发送的正文。"

    class CapturingRenderer(PlanRenderer):
        captured = None

        def render(self, draft, **kwargs):
            self.captured = draft
            return super().render(draft, **kwargs)

    draft = _draft()
    renderer = CapturingRenderer(
        PROJECT_ROOT / "app" / "rendering" / "templates"
    )

    rendered = asyncio.run(
        renderer.render_with_optional_polish(
            draft,
            provider=SuccessfulProvider(),
        )
    )

    assert rendered.polish_applied is True
    assert renderer.captured is not None
    polished_plan = renderer.captured.plan
    assert polished_plan.communication_text == "这是润色后且可直接发送的正文。"
    assert (
        polished_plan.communication_guide.message
        == polished_plan.communication_text
    )
    assert (
        polished_plan.communication_guide.model_dump(exclude={"message"})
        == draft.plan.communication_guide.model_dump(exclude={"message"})
    )
    assert polished_plan.model_dump(
        exclude={"communication_text", "communication_guide"}
    ) == draft.plan.model_dump(
        exclude={"communication_text", "communication_guide"}
    )
