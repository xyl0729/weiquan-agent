from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.errors import (
    DataIntegrityError,
    ProviderError,
    ProviderOutputError,
    SessionNotFoundError,
)
from app.agent.models import CaseContinuationResult
from app.agent.pipeline import PIPELINE_STAGES, ConsultationPipeline
from app.config import Settings
from app.db.session import SessionStore
from app.jurisdiction.rules import JurisdictionRegistry
from app.limits.usage import ProviderUsageControls
from app.playbooks.registry import PlaybookRegistry
from app.providers.fake import FakeProvider
from app.rendering.renderer import PlanRenderer


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(coroutine):
    return asyncio.run(coroutine)


def make_pipeline(
    tmp_path: Path,
    *,
    provider: FakeProvider | None = None,
    statute_path: Path | None = None,
    usage_controls: ProviderUsageControls | None = None,
) -> tuple[ConsultationPipeline, SessionStore]:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        db_path=tmp_path / "app.db",
        statutes_db_path=(
            statute_path
            or PROJECT_ROOT / "data" / "statutes.db"
        ),
    )
    store = SessionStore(
        settings.database_path,
        ttl_hours=settings.session_ttl_hours,
    )
    store.initialize()
    pipeline = ConsultationPipeline(
        settings=settings,
        store=store,
        registry=PlaybookRegistry.from_directory(
            PROJECT_ROOT / "app" / "playbooks"
        ),
        provider=provider or FakeProvider(),
        jurisdictions=JurisdictionRegistry.from_path(
            PROJECT_ROOT / "app" / "jurisdiction" / "data.yaml"
        ),
        renderer=PlanRenderer(
            PROJECT_ROOT / "app" / "rendering" / "templates"
        ),
        usage_controls=usage_controls,
    )
    return pipeline, store


def test_followup_then_ready_plan_uses_all_fixed_stages(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)

    first = run(pipeline.consult(message="房东不退押金"))
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message=(
                "押金2000元，房东扣2000元，没理由，"
                "而且合同没写可以扣。"
            ),
            jurisdiction="CN",
        )
    )

    assert first.status == "need_more_facts"
    assert first.followup_round == 1
    assert first.can_ask_more is True
    assert first.questions
    assert first.draft is None

    assert second.status == "ready"
    assert second.draft is not None
    assert second.rendered is not None
    assert second.draft.plan.verdict.code == "deduction_lacks_stated_basis"
    assert second.rendered.plan_text.startswith("【立即保全证据】")
    assert [
        citation.ref for citation in second.draft.plan.citations
    ] == [
        basis.ref
        for basis in pipeline.registry.get(
            "deposit_deduction"
        ).legal_basis
    ]
    assert all(
        str(item.source_url).startswith("https://")
        for item in second.draft.plan.citations
    )
    assert provider.extraction_calls == 2
    assert provider.continuation_calls == 0

    audits = store.list_audit_records(audit_id=second.audit_id)
    assert [record.stage for record in audits] == list(PIPELINE_STAGES)
    assert all(record.status == "ok" for record in audits)


def test_two_followup_rounds_then_conservative_stop(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    first = run(pipeline.consult(message="房东不退押金"))
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="这些金额和合同情况我现在都不知道",
        )
    )
    third = run(
        pipeline.consult(
            session_id=first.session_id,
            message="仍然无法确认",
        )
    )

    assert second.followup_round == 2
    assert second.questions
    assert second.can_ask_more is False
    assert third.followup_round == 2
    assert third.questions == ()
    assert third.can_ask_more is False
    assert "两轮追问上限" in third.limitations[0]
    assert third.status == "need_more_facts"


def test_unknown_scenario_gets_safe_generic_question(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    result = run(pipeline.consult(message="我今天遇到一件很奇怪的事情"))

    assert result.status == "need_more_facts"
    assert result.scenario_id is None
    assert "纠纷属于" in result.questions[0]
    assert result.draft is None


@pytest.mark.parametrize(
    ("message", "scenario_id", "verdict", "expected_status"),
    [
        (
            "预付卡商家停业，未消费余额3000元",
            "prepaid_card",
            "unused_balance_refund_supported",
            "ready",
        ),
        (
            "公司强迫我加班，至今没有支付加班费",
            "overtime_pay",
            "forced_overtime_requires_review",
            "ready",
        ),
        (
            "网购商品有质量问题，商家拒绝退货，价款800元",
            "return_refused",
            "quality_remedies_available",
            "ready",
        ),
        (
            "我买到假货，商品以假充真，价款1200元",
            "counterfeit_goods",
            "counterfeit_sale_prohibited",
            "ready",
        ),
        (
            "培训机构停业，剩余学费3000元没有退",
            "training_refund",
            "unused_training_balance_refund_supported",
            "ready",
        ),
        (
            "会员自动续费前平台没有显著提醒，扣款99元",
            "auto_renewal",
            "prominent_renewal_notice_required",
            "ready",
        ),
        (
            "装修工程严重延期，催告后仍不施工",
            "renovation_default",
            "delayed_performance_may_support_termination",
            "ready",
        ),
        (
            "金额不大的简单民事案件，小额诉讼是不是一审终审，"
            "主张10000元",
            "small_claim_procedure",
            "eligibility_requires_local_threshold",
            "escalate",
        ),
    ],
)
def test_migrated_scenario_pipeline_smoke(
    tmp_path: Path,
    message: str,
    scenario_id: str,
    verdict: str,
    expected_status: str,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    result = run(
        pipeline.consult(
            message=message,
            jurisdiction="CN",
        )
    )

    assert result.scenario_id == scenario_id
    assert result.status == expected_status
    assert result.questions == ()
    assert result.draft is not None
    assert result.rendered is not None
    assert result.draft.plan.verdict.code == verdict
    assert [
        citation.ref for citation in result.draft.plan.citations
    ] == [
        basis.ref
        for basis in pipeline.registry.get(scenario_id).legal_basis
    ]
    if scenario_id == "small_claim_procedure":
        assert result.draft.plan.jurisdiction.status == "local_data_missing"


def test_existing_plan_uses_only_continuation_and_returns_short_answer(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)

    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="那不配合怎么办",
        )
    )
    third = run(
        pipeline.consult(
            session_id=first.session_id,
            message="他还是不配合怎么办",
        )
    )

    assert first.turn_kind == "initial_plan"
    assert first.draft is not None
    assert second.turn_kind == "followup_answer"
    assert second.draft is None
    assert second.reply is not None
    assert third.turn_kind == "followup_answer"
    assert third.draft is None
    assert third.reply is not None
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 2
    assert len(store.list_turns(first.session_id)) == 3
    allowed_citations = {
        basis.ref
        for basis in pipeline.registry.get("return_refused").legal_basis
    }
    assert set(second.reply.citation_refs) <= allowed_citations
    assert set(third.reply.citation_refs) <= allowed_citations
    assert {
        citation.ref for citation in second.reply_citations
    } == set(second.reply.citation_refs)
    assert {
        citation.ref for citation in third.reply_citations
    } == set(third.reply.citation_refs)


def test_changed_continuation_facts_recompute_plan_locally(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        continuation_responses=[
            CaseContinuationResult(
                route="same_case",
                scenario_id="return_refused",
                facts={"purchase_amount": 1200},
                answer="金额已经补充。",
                confidence=0.99,
                provider="fake",
                model="fake-deterministic-v1",
            )
        ]
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )

    updated = run(
        pipeline.consult(
            session_id=first.session_id,
            message="更正一下，商品价款是1200元",
        )
    )

    assert updated.turn_kind == "plan_update"
    assert updated.draft is not None
    assert updated.reply is None
    assert store.require_session(first.session_id).facts[
        "purchase_amount"
    ] == 1200
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 1


def test_cleared_optional_fact_recomputes_plan_locally(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        continuation_responses=[
            CaseContinuationResult(
                route="same_case",
                scenario_id="return_refused",
                cleared_slots=["purchase_amount"],
                answer="已撤回尚未确认的商品金额。",
                confidence=0.99,
                provider="fake",
                model="fake-deterministic-v1",
            )
        ]
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )

    updated = run(
        pipeline.consult(
            session_id=first.session_id,
            message="金额还没确认，先撤回800元这个信息",
        )
    )

    assert updated.turn_kind == "plan_update"
    assert updated.draft is not None
    assert "purchase_amount" not in store.require_session(
        first.session_id
    ).facts
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 1


@pytest.mark.parametrize(
    "continuation_fields",
    [
        {"facts": {"undeclared_slot": "unsafe"}},
        {"action_refs": ["A999"]},
        {"citation_refs": ["住房租赁条例.第十条"]},
    ],
    ids=["slot", "action", "cross-scenario-citation"],
)
def test_invalid_continuation_references_leave_case_unchanged(
    tmp_path: Path,
    continuation_fields: dict[str, object],
) -> None:
    provider = FakeProvider(
        continuation_responses=[
            CaseContinuationResult(
                route="same_case",
                scenario_id="return_refused",
                answer="继续处理。",
                confidence=0.99,
                provider="fake",
                model="fake-deterministic-v1",
                **continuation_fields,
            )
        ]
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    before = store.require_session(first.session_id)

    with pytest.raises(ProviderOutputError):
        run(
            pipeline.consult(
                session_id=first.session_id,
                message="对方仍然拒绝处理",
            )
        )

    after = store.require_session(first.session_id)
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.status == before.status
    assert len(store.list_turns(first.session_id)) == 1
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 1


def test_low_confidence_unsupported_new_case_is_rejected(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        continuation_responses=[
            CaseContinuationResult(
                route="new_case",
                scenario_id="unsupported",
                confidence=0.2,
                provider="fake",
                model="fake-deterministic-v1",
            )
        ]
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    before = store.require_session(first.session_id)

    with pytest.raises(ProviderOutputError):
        run(
            pipeline.consult(
                session_id=first.session_id,
                message="另外还有一件说不清的事",
            )
        )

    after = store.require_session(first.session_id)
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.status == before.status
    assert len(store.list_turns(first.session_id)) == 1


def test_new_dispute_preserves_current_case_state(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        continuation_responses=[
            CaseContinuationResult(
                route="new_case",
                scenario_id="deposit_deduction",
                confidence=0.99,
                provider="fake",
                model="fake-deterministic-v1",
            )
        ]
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    before = store.require_session(first.session_id)

    result = run(
        pipeline.consult(
            session_id=first.session_id,
            message="另外房东还扣了我的租房押金",
        )
    )
    after = store.require_session(first.session_id)

    assert result.turn_kind == "new_case"
    assert result.draft is None
    assert result.reply is not None
    assert result.reply.new_case == {
        "scenario_id": "deposit_deduction",
        "label": pipeline.registry.get("deposit_deduction").name,
    }
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.status == before.status


def test_continuation_failure_keeps_prior_case_and_turns(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    before = store.require_session(first.session_id)
    provider._error = ProviderError(  # noqa: SLF001 - injected fault
        "provider_timeout",
        retryable=True,
    )

    with pytest.raises(ProviderError):
        run(
            pipeline.consult(
                session_id=first.session_id,
                message="他不配合怎么办",
            )
        )

    after = store.require_session(first.session_id)
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.status == before.status
    assert len(store.list_turns(first.session_id)) == 1


def test_unknown_session_is_rejected(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    with pytest.raises(SessionNotFoundError):
        run(
            pipeline.consult(
                session_id=str(uuid4()),
                message="房东不退押金",
            )
        )


def test_provider_failure_is_safe_and_not_downgraded_to_fake(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        error=ProviderError(
            "provider_timeout",
            retryable=True,
        )
    )
    pipeline, _ = make_pipeline(tmp_path, provider=provider)

    with pytest.raises(ProviderError) as exc_info:
        run(pipeline.consult(message="房东不退押金"))

    assert exc_info.value.category == "provider_timeout"
    assert "房东" not in exc_info.value.safe_message


def test_missing_statute_database_fails_closed(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        statute_path=tmp_path / "missing-statutes.db",
    )

    with pytest.raises(DataIntegrityError, match="法条检索失败"):
        run(
            pipeline.consult(
                message=(
                    "押金2000元，房东扣2000元，没理由，"
                    "合同没写可以扣。"
                )
            )
        )
