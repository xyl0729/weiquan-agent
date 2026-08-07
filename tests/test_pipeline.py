from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.errors import (
    DataIntegrityError,
    ProviderError,
    SessionNotFoundError,
)
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
    pipeline, store = make_pipeline(tmp_path)

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
    assert len(second.draft.plan.citations) >= 7
    assert all(
        str(item.source_url).startswith("https://")
        for item in second.draft.plan.citations
    )

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
    if scenario_id == "small_claim_procedure":
        assert result.draft.plan.jurisdiction.status == "local_data_missing"


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
