from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.errors import (
    CaseNoProgressError,
    CircuitTrippedError,
    ConsultationConflictError,
    DataIntegrityError,
    ProviderError,
    ProviderOutputError,
    SessionNotFoundError,
    StorageUnavailableError,
)
from app.agent.models import CaseContinuationResult, ExtractionResult, UsageInfo
from app.agent.pipeline import PIPELINE_STAGES, ConsultationPipeline
from app.agent.progression import normalize_visible_text, project_response
from app.attachments.errors import (
    AttachmentResourceLimitError,
    AttachmentStateConflictError,
)
from app.attachments.models import (
    ExtractionBlock,
    ExtractionResult as AttachmentExtractionResult,
)
from app.attachments.store import AttachmentStore
from app.config import Settings
from app.db.session import SessionStore
from app.jurisdiction.rules import JurisdictionRegistry
from app.limits.usage import ProviderUsageControls
from app.limits.reservations import (
    InMemoryQuotaStore,
    QuotaCallController,
    QuotaService,
)
from app.playbooks.registry import PlaybookRegistry
from app.providers.catalog import (
    ProviderCatalog,
    ProviderCatalogEntry,
    ProviderResolver,
)
from app.providers.fake import FakeProvider
from app.rendering.renderer import PlanRenderer


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(coroutine):
    return asyncio.run(coroutine)


def make_pipeline(
    tmp_path: Path,
    *,
    provider: FakeProvider | None = None,
    provider_resolver: ProviderResolver | None = None,
    statute_path: Path | None = None,
    usage_controls: ProviderUsageControls | None = None,
    llm_total_timeout_seconds: float = 30.0,
) -> tuple[ConsultationPipeline, SessionStore]:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        db_path=tmp_path / "app.db",
        statutes_db_path=(
            statute_path
            or PROJECT_ROOT / "data" / "statutes.db"
        ),
        llm_total_timeout_seconds=llm_total_timeout_seconds,
    )
    store = SessionStore(
        settings.database_path,
        ttl_hours=settings.session_ttl_hours,
    )
    store.initialize()
    attachments = AttachmentStore(
        store,
        draft_ttl_seconds=settings.attachment_draft_ttl_seconds,
    )
    pipeline = ConsultationPipeline(
        settings=settings,
        store=store,
        attachments=attachments,
        registry=PlaybookRegistry.from_directory(
            PROJECT_ROOT / "app" / "playbooks"
        ),
        provider=provider or (
            None if provider_resolver is not None else FakeProvider()
        ),
        provider_resolver=provider_resolver,
        jurisdictions=JurisdictionRegistry.from_path(
            PROJECT_ROOT / "app" / "jurisdiction" / "data.yaml"
        ),
        renderer=PlanRenderer(
            PROJECT_ROOT / "app" / "rendering" / "templates"
        ),
        usage_controls=usage_controls,
    )
    return pipeline, store


def make_switching_resolver(
    fake: FakeProvider,
    deepseek: FakeProvider,
) -> ProviderResolver:
    return ProviderResolver(
        ProviderCatalog(
            [
                ProviderCatalogEntry(
                    id="fake",
                    display_name="离线演示",
                    model=fake.model,
                    available=True,
                    offline=True,
                    is_default=True,
                ),
                ProviderCatalogEntry(
                    id="deepseek",
                    display_name="DeepSeek",
                    model=deepseek.model,
                    available=True,
                    offline=False,
                    is_default=False,
                ),
            ]
        ),
        {"fake": fake, "deepseek": deepseek},
    )


def create_confirmed_attachment(
    pipeline: ConsultationPipeline,
    *,
    text: str = "订单金额 299 元",
    name: str = "证据.pdf",
) -> str:
    processing = pipeline.attachments.create_processing(
        original_name=name,
        media_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
    )
    pipeline.attachments.save_extraction(
        processing.id,
        AttachmentExtractionResult(
            media_type="application/pdf",
            page_count=1,
            extraction_method="direct_text",
            blocks=(
                ExtractionBlock(
                    page_number=1,
                    block_index=0,
                    text=text,
                    confidence=0.98,
                ),
            ),
            warnings=("review_amount",),
        ),
    )
    return pipeline.attachments.confirm(processing.id, text).id


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
    payload = second.public_payload()
    assert payload["plan"]["communication_guide"]["recipient"]
    assert (
        payload["plan"]["communication_text"]
        == payload["plan"]["communication_guide"]["message"]
    )
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


def test_unknown_scenario_gets_bounded_unverified_guidance(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    result = run(pipeline.consult(message="我今天遇到一件很奇怪的事情"))

    assert result.status == "need_more_facts"
    assert result.scenario_id is None
    assert result.turn_kind == "unverified_guidance"
    assert result.coverage is not None
    assert result.coverage.mode == "unverified_guidance"
    assert result.coverage.topic_id == "unknown"
    assert result.guidance is not None
    assert result.guidance.next_question
    assert result.questions == ()
    assert result.draft is None
    assert result.rendered is None
    payload = result.public_payload()
    assert payload["verdict"] is None
    assert payload["plan"] is None
    assert payload["citations"] == []


def test_unverified_medical_topic_returns_general_basis_without_formal_artifacts(
    tmp_path: Path,
) -> None:
    pipeline, store = make_pipeline(tmp_path)

    result = run(
        pipeline.consult(
            message="医院不肯给我病历，医疗收费涉及300元也说不清",
        )
    )

    assert result.turn_kind == "unverified_guidance"
    assert result.coverage is not None
    assert result.coverage.topic_id == "medical_service_dispute"
    assert result.coverage.playbook_id is None
    assert result.guidance is not None
    assert result.guidance.communication_guide.recipient
    assert result.scenario_id is None
    assert result.draft is None
    assert result.rendered is None
    assert result.reply is None
    assert [citation.ref for citation in result.reply_citations] == [
        "民法典.第一千二百二十五条",
        "民法典.第一千二百二十二条",
    ]
    assert all(
        citation.basis_scope == "general"
        and citation.applicability_notice
        for citation in result.reply_citations
    )
    payload = result.public_payload()
    assert payload["verdict"] is None
    assert payload["plan"] is None
    assert store.require_session(result.session_id).facts == {
        "amount": 300.0,
    }


def test_unverified_privacy_and_reputation_returns_only_relevant_general_basis(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    result = run(
        pipeline.consult(
            message="对方在网上公开我的个人信息并造谣，我应该怎么处理？",
        )
    )

    assert result.turn_kind == "unverified_guidance"
    assert result.coverage is not None
    assert result.coverage.topic_id == "privacy_reputation"
    assert result.coverage.playbook_id is None
    assert result.scenario_id is None
    assert result.draft is None
    assert result.rendered is None
    assert [citation.ref for citation in result.reply_citations] == [
        "民法典.第一千零二十四条",
        "民法典.第一千零三十四条",
        "民法典.第一千零三十五条",
    ]
    assert all(
        citation.basis_scope == "general"
        and citation.applicability_notice
        for citation in result.reply_citations
    )
    payload = result.public_payload()
    assert payload["verdict"] is None
    assert payload["plan"] is None


def test_wrong_item_goal_and_no_response_followups_use_context(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)

    first = run(
        pipeline.consult(
            message=(
                "网购时商家给我发错货了，我想让商家补发正确商品，"
                "应该怎么处理？"
            )
        )
    )
    before = store.require_session(first.session_id)
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="我希望商家重新发货",
        )
    )
    third = run(
        pipeline.consult(
            session_id=first.session_id,
            message="如果他不理我怎么办",
        )
    )
    after = store.require_session(first.session_id)

    assert first.turn_kind == "initial_plan"
    assert first.draft is not None
    assert first.draft.plan.scenario_name == "网购商品交付与退换纠纷"
    assert [citation.ref for citation in first.draft.plan.citations] == [
        "民法典.第五百零九条",
        "民法典.第五百七十七条",
    ]
    assert "补发正确商品" in first.draft.plan.summary
    assert all(
        "退款" not in action.text
        for action in first.draft.plan.actions
    )
    assert "已经明确选择的处理诉求" in (
        first.draft.plan.communication_text
    )
    assert second.turn_kind == "followup_answer"
    assert second.reply is not None
    assert "首要诉求" in second.reply.text
    assert "不必先改成退款" in second.reply.text
    assert "如果还没正式提出" in second.reply.text
    assert "退款还是补发" not in second.reply.text
    assert "第 " not in second.reply.text
    assert "。；" not in second.reply.text
    assert third.reply is not None
    assert "订单售后或平台客服" in third.reply.text
    assert "错发商品、面单和催告记录" in third.reply.text
    assert "消费投诉渠道" in third.reply.text
    assert before.facts == after.facts
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 2
    assert provider.composition_calls == 3


def test_food_foreign_object_followups_keep_evidence_and_amount_boundary(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)

    first = run(pipeline.consult(message="30元外卖里有虫子"))
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="已经拍照但吃掉一部分",
        )
    )
    third = run(
        pipeline.consult(
            session_id=first.session_id,
            message="可以要求多少",
        )
    )

    assert first.turn_kind == "unverified_guidance"
    assert first.coverage is not None
    assert first.coverage.topic_id == "logistics_travel_food"
    assert first.guidance is not None
    assert "停止食用" in first.guidance.direct_answer
    assert second.reply is not None
    assert "吃掉一部分" in second.reply.text
    assert "实物状态改变" in second.reply.text
    assert "现在进入第" not in second.reply.text
    assert third.reply is not None
    assert "实际损失" in third.reply.text
    assert "价款十倍" in third.reply.text
    assert "损失三倍" in third.reply.text
    assert "一千元" in third.reply.text
    assert "不等于这些条件自动成立" in third.reply.text
    citations = (
        *first.reply_citations,
        *second.reply_citations,
        *third.reply_citations,
    )
    assert citations
    assert all(item.basis_scope == "general" for item in citations)
    assert all(item.applicability_notice for item in citations)
    food_citations = [
        item
        for item in third.reply_citations
        if item.ref == "食品安全法.第一百四十八条"
    ]
    assert len(food_citations) == 1
    assert "不符合食品安全标准" in food_citations[0].applicability_notice
    assert "不等于自动适用" in food_citations[0].applicability_notice
    assert provider.extraction_calls == 3
    assert provider.continuation_calls == 0
    assert provider.composition_calls == 3


def test_second_stage_failure_returns_local_answer_and_refunds_quota(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        composition_error=ProviderError(
            "provider_timeout",
            retryable=True,
        )
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    quota = QuotaService(InMemoryQuotaStore())
    subject_id = "66666666-6666-4666-8666-666666666666"
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id=subject_id,
        logical_call_id=str(uuid4()),
    )

    result = run(
        pipeline.consult(
            message="30元外卖里有虫子",
            quota_call=controller,
        )
    )

    assert result.turn_kind == "unverified_guidance"
    assert result.guidance is not None
    assert "停止食用" in result.guidance.direct_answer
    assert result.reply_citations
    assert provider.extraction_calls == 1
    assert provider.composition_calls == 1
    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    composition_audit = next(
        record
        for record in store.list_audit_records(audit_id=result.audit_id)
        if record.stage == "composition"
    )
    assert composition_audit.status == "degraded"
    assert composition_audit.error_category == "provider_timeout"


def test_second_stage_circuit_trip_keeps_first_stage_and_local_answer(
    tmp_path: Path,
) -> None:
    class TripOnSecondProviderCall:
        provider = "fake"

        def __init__(self) -> None:
            self.before_calls = 0

        def before_call(self, client_identifier: str) -> None:
            assert client_identifier
            self.before_calls += 1
            if self.before_calls == 2:
                raise CircuitTrippedError()

        def after_call(
            self,
            client_identifier: str,
            usage: UsageInfo,
        ) -> UsageInfo:
            assert client_identifier
            return usage

    controls = TripOnSecondProviderCall()
    provider = FakeProvider()
    pipeline, store = make_pipeline(
        tmp_path,
        provider=provider,
        usage_controls=controls,  # type: ignore[arg-type]
    )
    quota = QuotaService(InMemoryQuotaStore())
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id="77777777-7777-4777-8777-777777777777",
        logical_call_id=str(uuid4()),
    )

    result = run(
        pipeline.consult(
            message="30元外卖里有虫子，可以要求多少",
            quota_call=controller,
        )
    )

    assert result.guidance is not None
    assert "价款十倍" in result.guidance.direct_answer
    assert "一千元" in result.guidance.direct_answer
    assert any(
        item.ref == "食品安全法.第一百四十八条"
        and item.basis_scope == "general"
        for item in result.reply_citations
    )
    assert controls.before_calls == 2
    assert provider.extraction_calls == 1
    assert provider.composition_calls == 0
    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    composition_audit = next(
        record
        for record in store.list_audit_records(audit_id=result.audit_id)
        if record.stage == "composition"
    )
    assert composition_audit.status == "degraded"
    assert composition_audit.error_category == "circuit_tripped"


def test_tiny_shared_budget_keeps_local_answer_without_whole_turn_fallback(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(
        tmp_path,
        provider=provider,
        llm_total_timeout_seconds=0.1,
    )

    result = run(pipeline.consult(message="30元外卖里有虫子"))

    assert result.turn_kind == "unverified_guidance"
    assert result.coverage is not None
    assert result.coverage.topic_id == "logistics_travel_food"
    assert result.guidance is not None
    assert "停止食用" in result.guidance.direct_answer
    assert provider.extraction_calls == 1
    assert provider.composition_calls == 0
    composition_audit = next(
        record
        for record in store.list_audit_records(audit_id=result.audit_id)
        if record.stage == "composition"
    )
    assert composition_audit.status == "degraded"
    assert composition_audit.error_category == "provider_timeout"


def test_game_account_followup_calls_provider_with_conversation_context(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first_message = (
        "我把游戏账号借给网友了，里面充值了4000元，"
        "他用我的账号开挂导致账号被封了十年"
    )
    second_message = (
        "我联系不上人，他把我删除了我该怎么办，"
        "能在法院直接起诉他吗"
    )

    first = run(pipeline.consult(message=first_message))
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message=second_message,
        )
    )

    assert first.turn_kind == "unverified_guidance"
    assert first.coverage is not None
    assert first.coverage.topic_id == "game_account_dispute"
    assert second.turn_kind == "followup_answer"
    assert second.reply is not None
    assert "暂时不足以判断能否直接起诉" in second.reply.text
    assert "诈骗" not in second.reply.text
    assert second.reply.suggested_actions == (
        "立即修改密码、退出其他设备登录并保护验证码和绑定信息",
        "通过游戏平台官方入口申诉封禁，要求核对登录、违规和处罚记录",
    )
    assert provider.extraction_calls == 2
    assert provider.continuation_calls == 0

    second_context = provider.extraction_context_calls[1]
    assert second_context["previous_topic_id"] == "game_account_dispute"
    assert second_context["previous_topic_label"] == (
        "游戏账号借用、封禁与平台申诉"
    )
    assert second_context["confirmed_facts"] == {"amount": 4000.0}
    assert second_context["is_followup"] is True
    assert second_context["is_direct_question"] is True
    recent = second_context["recent_conversation"]
    assert isinstance(recent, list)
    assert len(recent) == 1
    assert recent[0]["user_message"] == first_message
    assert recent[0]["assistant_reply"]

    turns = store.list_turns(first.session_id)
    assert len(turns) == 2
    assert turns[-1].response["coverage"] is None
    assert turns[-1].response["turn_kind"] == "followup_answer"


def test_direct_question_without_model_answer_returns_local_guidance(
    tmp_path: Path,
) -> None:
    first_extraction = ExtractionResult(
        candidate_topic_id="game_account_dispute",
        topic_label="游戏账号借用、封禁与平台申诉",
        facts={"amount": 4000.0},
        confidence=0.96,
        provider="fake",
        model="fake-deterministic-v1",
        request_id="fake-first",
        usage=UsageInfo(),
    )
    missing_answer = ExtractionResult(
        candidate_topic_id="game_account_dispute",
        topic_label="游戏账号借用、封禁与平台申诉",
        facts={},
        confidence=0.96,
        provider="fake",
        model="fake-deterministic-v1",
        request_id="fake-missing-answer",
        usage=UsageInfo(),
    )
    provider = FakeProvider(responses=[first_extraction, missing_answer])
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="游戏账号借给网友后被开挂封禁，里面充值了4000元",
        )
    )

    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message=(
                "我只有微信转账记录和聊天记录，没有对方身份证号，"
                "该怎么办？"
            ),
        )
    )

    assert second.turn_kind == "unverified_guidance"
    assert second.guidance is not None
    assert second.guidance.direct_answer is not None
    assert "游戏账号" in second.guidance.direct_answer
    assert second.provider_request_id is None
    visible = project_response(second.public_payload())
    assert all("现在进入第" not in unit for unit in visible.all_units)
    extraction_audit = next(
        record
        for record in store.list_audit_records(audit_id=second.audit_id)
        if record.stage == "extraction"
    )
    assert extraction_audit.status == "degraded"
    assert extraction_audit.error_category == "provider_invalid_output"


def test_first_direct_question_keeps_guidance_and_answer_in_context(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    first_message = (
        "我把游戏账号借给网友，他开挂导致封禁，"
        "我现在能直接去法院起诉他吗？"
    )

    first = run(pipeline.consult(message=first_message))

    assert first.turn_kind == "unverified_guidance"
    assert first.coverage is not None
    assert first.coverage.topic_id == "game_account_dispute"
    assert first.guidance is not None
    assert first.guidance.direct_answer is not None
    assert "暂时不足以判断能否直接起诉" in (
        first.guidance.direct_answer
    )
    assert first.reply is None
    first_payload = first.public_payload()
    assert first_payload["guidance"]["direct_answer"] == (
        first.guidance.direct_answer
    )
    assert normalize_visible_text(
        first.guidance.direct_answer
    ) in project_response(first_payload).replies

    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="他现在把我删除了，我接下来该怎么办？",
        )
    )

    assert second.turn_kind == "followup_answer"
    second_context = provider.extraction_context_calls[1]
    recent = second_context["recent_conversation"]
    assert isinstance(recent, list)
    assert normalize_visible_text(
        first.guidance.direct_answer
    ) in recent[0]["assistant_reply"]


def test_unverified_conversation_switches_only_on_explicit_new_topic(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="游戏账号借给网友后，他开挂导致账号被封禁",
        )
    )

    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="另外一件事，房东一直不维修，我想提前退租",
        )
    )

    assert second.turn_kind == "unverified_guidance"
    assert second.coverage is not None
    assert second.coverage.topic_id == "general_rental"
    assert provider.extraction_calls == 2


def test_unverified_bare_continue_keeps_current_stage_and_still_replies(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="医院不肯给我病历，医疗收费也说不清",
        )
    )

    replies = []
    for _ in range(6):
        current = run(
            pipeline.consult(
                session_id=first.session_id,
                message="继续",
            )
        )
        replies.append(current)

    assert all(item.turn_kind == "followup_answer" for item in replies)
    assert all(item.coverage is None for item in replies)
    assert all(item.guidance is None for item in replies)
    assert all("如果其实已经做过" in item.reply.text for item in replies)
    assert all("说明对方怎样回复" in item.reply.text for item in replies)
    assert all("第 " not in item.reply.text for item in replies)
    assert len({item.reply.text for item in replies}) == 1
    assert provider.extraction_calls == 7
    final = run(
        pipeline.consult(
            session_id=first.session_id,
            message="继续",
        )
    )
    assert final.turn_kind == "followup_answer"
    assert provider.extraction_calls == 8
    assert len(store.list_turns(first.session_id)) == 8


@pytest.mark.parametrize(
    ("message", "expected_fragment"),
    [
        ("我已经发送了", "重点记录对方回复"),
        ("对方一直没回复", "在原渠道书面催办"),
        ("对方明确拒绝处理", "通过上一级或主管渠道升级反映"),
        ("我已经投诉了", "现有处理渠道已经走完"),
    ],
)
def test_unverified_status_update_jumps_to_next_stage_with_provider(
    tmp_path: Path,
    message: str,
    expected_fragment: str,
) -> None:
    provider = FakeProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="医院不肯给我病历，医疗收费也说不清",
        )
    )

    result = run(
        pipeline.consult(
            session_id=first.session_id,
            message=message,
        )
    )

    assert result.turn_kind == "followup_answer"
    assert expected_fragment in result.reply.text
    assert result.coverage is None
    assert result.guidance is None
    assert result.provider_request_id is not None
    assert provider.extraction_calls == 2


def test_emergency_gate_calls_provider_and_keeps_local_safety_guidance(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)

    result = run(
        pipeline.consult(
            message="孩子正在被打，未成年人正在受伤害",
        )
    )

    assert result.turn_kind == "emergency_guidance"
    assert result.status == "escalate"
    assert result.coverage is not None
    assert result.coverage.mode == "emergency_guidance"
    assert "minor_harm" in result.coverage.risk_flags
    assert result.guidance is not None
    assert "不要为了取证" in result.guidance.limitations[0]
    assert result.provider_name == provider.name
    assert result.provider_model == provider.model
    assert result.provider_request_id is not None
    assert result.usage.total_tokens == 0
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 0


def test_repeated_emergency_returns_new_status_question_not_full_card(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="孩子正在被打，未成年人正在受伤害",
        )
    )
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="孩子还是正在被打",
        )
    )

    assert first.turn_kind == "emergency_guidance"
    assert second.turn_kind == "followup_answer"
    assert second.guidance is None
    assert second.coverage is None
    assert second.reply is not None
    assert "是否" in second.reply.text
    assert second.reply.text != first.guidance.next_question
    assert second.provider_request_id is not None
    assert provider.extraction_calls == 2


def test_resolved_emergency_resumes_unverified_stage(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="医院不肯给我病历，医疗收费也说不清",
        )
    )
    emergency = run(
        pipeline.consult(
            session_id=first.session_id,
            message="我现在正在被打，无法离开现场",
        )
    )
    resumed = run(
        pipeline.consult(
            session_id=first.session_id,
            message="现在安全了，风险解除",
        )
    )

    assert emergency.turn_kind == "emergency_guidance"
    assert resumed.turn_kind == "followup_answer"
    assert resumed.reply is not None
    assert "紧急风险解除" in resumed.reply.text
    assert "医疗服务纠纷" in resumed.reply.text
    assert resumed.provider_request_id is not None
    assert provider.extraction_calls == 3


def test_emergency_turn_preserves_existing_formal_case_state(
    tmp_path: Path,
) -> None:
    continuation_contexts = []

    class CapturingProvider(FakeProvider):
        async def continue_case(
            self,
            message,
            context,
            evidence=(),
            *,
            timeout_seconds=None,
        ):
            continuation_contexts.append(context)
            return await super().continue_case(
                message,
                context,
                evidence,
                timeout_seconds=timeout_seconds,
            )

    provider = CapturingProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    before = store.require_session(first.session_id)

    emergency = run(
        pipeline.consult(
            session_id=first.session_id,
            message="我现在正在被打，无法离开现场",
        )
    )
    after = store.require_session(first.session_id)

    assert emergency.turn_kind == "emergency_guidance"
    assert emergency.scenario_id is None
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.followup_round == before.followup_round
    assert after.status == before.status
    assert after.jurisdiction == before.jurisdiction
    assert provider.extraction_calls == 2
    assert provider.continuation_calls == 0

    continued = run(
        pipeline.consult(
            session_id=first.session_id,
            message="商家还是不配合怎么办",
        )
    )
    assert continued.turn_kind == "followup_answer"
    assert continued.provider_request_id is not None
    assert provider.continuation_calls == 1
    assert [
        turn.turn_kind
        for turn in continuation_contexts[0].recent_turns
    ] == ["initial_plan", "emergency_guidance"]


def test_unverified_facts_are_filtered_before_entering_formal_playbook(
    tmp_path: Path,
) -> None:
    pipeline, store = make_pipeline(tmp_path)
    first = run(
        pipeline.consult(
            message="很少见的事情涉及300元，目前不知道属于什么问题",
        )
    )

    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message=(
                "网购商品有质量问题，商家拒绝退货，商品价款800元"
            ),
            jurisdiction="CN",
        )
    )

    assert first.turn_kind == "unverified_guidance"
    assert store.list_turns(first.session_id)[0].facts == {
        "amount": 300.0,
    }
    assert second.turn_kind == "initial_plan"
    assert second.scenario_id == "return_refused"
    assert store.require_session(first.session_id).facts == {
        "issue_type": "quality_problem",
        "purchase_amount": 800.0,
    }


def test_provider_can_switch_between_turns_in_one_session(
    tmp_path: Path,
) -> None:
    class DeepSeekLikeFake(FakeProvider):
        name = "deepseek"
        model = "deepseek-test"

    fake = FakeProvider()
    deepseek = DeepSeekLikeFake()
    resolver = make_switching_resolver(fake, deepseek)
    pipeline, store = make_pipeline(
        tmp_path,
        provider_resolver=resolver,
    )

    first = run(
        pipeline.consult(
            message="房东不退押金",
            provider_id="fake",
        )
    )
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message=(
                "押金2000元，房东扣2000元，没理由，"
                "合同没写可以扣。"
            ),
            provider_id="deepseek",
            jurisdiction="CN",
        )
    )

    assert first.provider_name == "fake"
    assert second.provider_name == "deepseek"
    assert second.provider_model == "deepseek-test"
    assert second.scenario_id == "deposit_deduction"
    assert fake.extraction_calls == 1
    assert deepseek.extraction_calls == 1
    turns = store.list_turns(first.session_id)
    assert [turn.provider_name for turn in turns] == ["fake", "deepseek"]


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
    playbook = pipeline.registry.get(scenario_id)
    selected_rule_id = result.draft.plan.verdict.rule_ids[0]
    assert [
        citation.ref for citation in result.draft.plan.citations
    ] == playbook.legal_refs_for_rule(selected_rule_id)
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


def test_bare_continue_advances_distinct_actions_with_provider_call(
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
            message="继续",
        )
    )
    third = run(
        pipeline.consult(
            session_id=first.session_id,
            message="然后呢",
        )
    )

    assert second.turn_kind == "followup_answer"
    assert third.turn_kind == "followup_answer"
    assert second.reply is not None
    assert third.reply is not None
    assert len(second.reply.suggested_actions) == 2
    assert len(third.reply.suggested_actions) == 1
    assert (
        second.reply.suggested_actions
        != third.reply.suggested_actions
    )
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 2
    assert second.provider_request_id is not None
    assert third.provider_request_id is not None
    assert len(store.list_turns(first.session_id)) == 3


def test_duplicate_provider_reply_is_repaired_without_provider_retry(
    tmp_path: Path,
) -> None:
    duplicate = CaseContinuationResult(
        route="same_case",
        scenario_id="return_refused",
        answer="先保存商家拒绝处理的记录，再按现有方案继续推进。",
        action_refs=["A1"],
        confidence=0.99,
        provider="fake",
        model="fake-deterministic-v1",
    )
    provider = FakeProvider(
        continuation_responses=[duplicate, duplicate],
    )
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
            message="商家还是不配合怎么办",
        )
    )

    repaired = run(
        pipeline.consult(
            session_id=first.session_id,
            message="平台也没有处理，下一步怎么办",
        )
    )

    assert second.reply is not None
    assert repaired.reply is not None
    assert repaired.reply.text != second.reply.text
    assert repaired.reply.suggested_actions != second.reply.suggested_actions
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 2
    assert len(store.list_turns(first.session_id)) == 3


def test_exhausted_continue_returns_explanation_and_refunds_quota(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    quota = QuotaService(InMemoryQuotaStore())
    subject_id = "55555555-5555-4555-8555-555555555555"
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    action_count = len(
        pipeline.registry.get("return_refused").actions
    )
    for _ in range(action_count):
        run(
            pipeline.consult(
                session_id=first.session_id,
                message="继续",
            )
        )
    turn_count = len(store.list_turns(first.session_id))
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id=subject_id,
        logical_call_id=str(uuid4()),
    )

    exhausted = run(
        pipeline.consult(
            session_id=first.session_id,
            message="继续",
            quota_call=controller,
        )
    )

    assert exhausted.reply is not None
    assert "没有可自动推进的新动作" in exhausted.reply.text
    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    assert quota.registered_status(subject_id).remaining_daily == 10
    assert len(store.list_turns(first.session_id)) == turn_count + 1
    assert provider.continuation_calls == action_count + 1


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
    assert updated.reply is not None
    assert "金额已经补充" in updated.reply.text
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
    assert updated.reply is not None
    assert "已撤回" in updated.reply.text
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
def test_invalid_continuation_references_degrade_and_leave_case_unchanged(
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

    result = run(
        pipeline.consult(
            session_id=first.session_id,
            message="对方仍然拒绝处理",
        )
    )

    after = store.require_session(first.session_id)
    assert result.turn_kind == "followup_answer"
    assert result.reply is not None
    assert result.reply.text
    assert result.coverage is not None
    assert result.coverage.mode == "formal"
    assert result.coverage.topic_id == "return_refused"
    assert result.provider_request_id is None
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.followup_round == before.followup_round
    assert after.status == before.status
    assert len(store.list_turns(first.session_id)) == 2
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 1


def test_low_confidence_unsupported_new_case_degrades_safely(
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

    result = run(
        pipeline.consult(
            session_id=first.session_id,
            message="另外还有一件说不清的事",
        )
    )

    after = store.require_session(first.session_id)
    assert result.turn_kind == "followup_answer"
    assert result.reply is not None
    assert result.reply.text
    assert result.coverage is not None
    assert result.coverage.mode == "formal"
    assert result.coverage.topic_id == "return_refused"
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.followup_round == before.followup_round
    assert after.status == before.status
    assert len(store.list_turns(first.session_id)) == 2


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

    result = run(
        pipeline.consult(
            session_id=first.session_id,
            message="他不配合怎么办",
        )
    )

    after = store.require_session(first.session_id)
    assert result.turn_kind == "followup_answer"
    assert result.reply is not None
    assert result.reply.text
    assert result.coverage is not None
    assert result.coverage.mode == "formal"
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.status == before.status
    assert len(store.list_turns(first.session_id)) == 2


def test_unknown_session_is_rejected(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    with pytest.raises(SessionNotFoundError):
        run(
            pipeline.consult(
                session_id=str(uuid4()),
                message="房东不退押金",
            )
        )


@pytest.mark.parametrize(
    "category",
    [
        "provider_invalid_output",
        "provider_timeout",
        "provider_network",
        "provider_busy",
        "provider_rate_limited",
        "provider_server_error",
        "provider_rejected",
        "provider_configuration",
    ],
)
def test_provider_failure_returns_safe_reply_without_fake_fallback(
    tmp_path: Path,
    category: str,
) -> None:
    provider = FakeProvider(
        error=ProviderError(
            category,
            retryable=True,
        )
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)

    result = run(pipeline.consult(message="房东不退押金"))
    session = store.require_session(result.session_id)

    assert result.turn_kind == "unverified_guidance"
    assert result.guidance is not None
    assert result.guidance.direct_answer
    assert result.guidance.next_question == "押金总额是多少元？"
    assert result.coverage is not None
    assert result.coverage.mode == "unverified_guidance"
    assert result.coverage.topic_id == "unknown"
    assert result.scenario_id is None
    assert result.status == "need_more_facts"
    assert session.scenario_id is None
    assert session.facts == {}
    assert session.status == "need_more_facts"
    assert result.provider_name == "fake"
    assert result.provider_request_id is None
    assert result.usage.total_tokens == 0
    assert len(store.list_turns(result.session_id)) == 1
    extraction_audit = next(
        record
        for record in store.list_audit_records(audit_id=result.audit_id)
        if record.stage == "extraction"
    )
    assert extraction_audit.status == "degraded"
    assert extraction_audit.error_category == category


def test_provider_failure_for_formal_case_asks_next_unasked_slot(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(pipeline.consult(message="房东不退押金"))
    before = store.require_session(first.session_id)
    provider._error = ProviderError(  # noqa: SLF001 - injected fault
        "provider_network",
        retryable=True,
    )

    result = run(
        pipeline.consult(
            session_id=first.session_id,
            message="接下来呢",
        )
    )
    after = store.require_session(first.session_id)

    assert result.turn_kind == "followup_answer"
    assert result.reply is not None
    assert "房东准备扣减或拒绝返还多少元？" in result.reply.text
    assert result.coverage is not None
    assert result.coverage.mode == "formal"
    assert result.scenario_id == "deposit_deduction"
    assert after.scenario_id == before.scenario_id
    assert after.facts == before.facts
    assert after.status == before.status


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


@pytest.mark.parametrize(
    ("message", "expected_turn_kind"),
    [
        ("房东不退押金", "fact_collection"),
        (
            "押金2000元，房东扣2000元，没理由，合同没写可以扣。",
            "initial_plan",
        ),
    ],
)
def test_new_consultation_attachment_uses_extraction_and_binds_turn(
    tmp_path: Path,
    message: str,
    expected_turn_kind: str,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    private_ocr_text = "OCR_PRIVATE_BODY_订单金额299元"
    attachment_id = create_confirmed_attachment(
        pipeline,
        text=private_ocr_text,
    )

    result = run(
        pipeline.consult(
            message=message,
            attachment_ids=[attachment_id],
        )
    )

    assert result.turn_kind == expected_turn_kind
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 0
    assert len(provider.extraction_evidence_calls) == 1
    assert [
        str(item.id) for item in provider.extraction_evidence_calls[0]
    ] == [attachment_id]
    assert provider.continuation_evidence_calls == []
    assert [str(item.id) for item in result.attachments] == [attachment_id]
    assert result.attachments[0].confirmed_text == private_ocr_text

    bound = pipeline.attachments.list_for_turn(result.turn_id)
    assert [item.id for item in bound] == [attachment_id]
    assert bound[0].status == "bound"

    stored_turn = store.list_turns(result.session_id)[0]
    assert stored_turn.user_message == message
    assert stored_turn.response["attachments"] == []
    assert private_ocr_text not in json.dumps(
        stored_turn.model_dump(mode="json"),
        ensure_ascii=False,
    )
    assert private_ocr_text not in json.dumps(
        [
            item.model_dump(mode="json")
            for item in store.list_audit_records(
                audit_id=result.audit_id
            )
        ],
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    "expected_turn_kind",
    ["followup_answer", "plan_update"],
)
def test_existing_plan_attachment_uses_only_continuation_and_binds_turn(
    tmp_path: Path,
    expected_turn_kind: str,
) -> None:
    continuation_responses = None
    if expected_turn_kind == "plan_update":
        continuation_responses = [
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
    provider = FakeProvider(
        continuation_responses=continuation_responses
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="网购商品有质量问题，商家拒绝退货，价款800元",
            jurisdiction="CN",
        )
    )
    attachment_id = create_confirmed_attachment(
        pipeline,
        text="平台拒绝退款记录",
    )

    result = run(
        pipeline.consult(
            session_id=first.session_id,
            message=(
                "商品价款更正为1200元"
                if expected_turn_kind == "plan_update"
                else "商家还是不配合怎么办"
            ),
            attachment_ids=[attachment_id],
        )
    )

    assert result.turn_kind == expected_turn_kind
    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 1
    assert provider.extraction_evidence_calls == [()]
    assert len(provider.continuation_evidence_calls) == 1
    assert [
        str(item.id)
        for item in provider.continuation_evidence_calls[0]
    ] == [attachment_id]
    assert [str(item.id) for item in result.attachments] == [attachment_id]
    assert [
        item.id for item in pipeline.attachments.list_for_turn(result.turn_id)
    ] == [attachment_id]
    assert store.list_turns(first.session_id)[-1].response[
        "attachments"
    ] == []


def test_new_case_turn_releases_attachment_without_binding(
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
    attachment_id = create_confirmed_attachment(
        pipeline,
        text="租房押金凭证",
    )

    result = run(
        pipeline.consult(
            session_id=first.session_id,
            message="另外房东还扣了我的租房押金",
            attachment_ids=[attachment_id],
        )
    )

    record = pipeline.attachments.get(attachment_id)
    assert result.turn_kind == "new_case"
    assert result.attachments == ()
    assert record.status == "confirmed"
    assert record.reservation_id is None
    assert record.session_id is None
    assert record.turn_id is None
    assert pipeline.attachments.list_for_turn(result.turn_id) == []
    assert len(store.list_turns(first.session_id)) == 2
    assert [
        str(item.id)
        for item in provider.continuation_evidence_calls[-1]
    ] == [attachment_id]


def test_attachment_reservation_released_after_provider_failure(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        error=ProviderError(
            "provider_timeout",
            retryable=True,
        )
    )
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    attachment_id = create_confirmed_attachment(pipeline)

    result = run(
        pipeline.consult(
            message="房东不退押金",
            attachment_ids=[attachment_id],
        )
    )

    record = pipeline.attachments.get(attachment_id)
    assert result.turn_kind == "unverified_guidance"
    assert result.attachments == ()
    assert record.status == "confirmed"
    assert record.reservation_id is None
    assert record.turn_id is None


def test_attachment_reservation_released_after_context_validation(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    attachment_id = create_confirmed_attachment(
        pipeline,
        text="证" * (pipeline.settings.max_attachment_context_chars + 1),
    )

    with pytest.raises(AttachmentResourceLimitError) as exc_info:
        run(
            pipeline.consult(
                message="房东不退押金",
                attachment_ids=[attachment_id],
            )
        )

    assert exc_info.value.code == "attachment_context_too_long"
    record = pipeline.attachments.get(attachment_id)
    assert record.status == "confirmed"
    assert record.reservation_id is None


def test_pipeline_concurrent_attachment_reservation_calls_provider_once(
    tmp_path: Path,
) -> None:
    class BlockingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.unblock = asyncio.Event()

        async def extract_facts(
            self,
            message,
            context,
            evidence=(),
            *,
            timeout_seconds=None,
        ):
            self.entered.set()
            await self.unblock.wait()
            return await super().extract_facts(
                message,
                context,
                evidence,
                timeout_seconds=timeout_seconds,
            )

    provider = BlockingProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    attachment_id = create_confirmed_attachment(pipeline)

    async def scenario():
        first = asyncio.create_task(
            pipeline.consult(
                message="房东不退押金",
                attachment_ids=[attachment_id],
            )
        )
        await provider.entered.wait()
        try:
            with pytest.raises(AttachmentStateConflictError):
                await pipeline.consult(
                    message="房东不退押金",
                    attachment_ids=[attachment_id],
                )
        finally:
            provider.unblock.set()
        return await first

    result = run(scenario())

    assert provider.extraction_calls == 1
    assert provider.continuation_calls == 0
    assert [
        item.id for item in pipeline.attachments.list_for_turn(result.turn_id)
    ] == [attachment_id]


def test_pipeline_cancellation_releases_attachment_reservation(
    tmp_path: Path,
) -> None:
    class BlockingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.never = asyncio.Event()

        async def extract_facts(
            self,
            message,
            context,
            evidence=(),
            *,
            timeout_seconds=None,
        ):
            del timeout_seconds
            del message, context, evidence
            self.entered.set()
            await self.never.wait()
            raise AssertionError("unreachable")

    provider = BlockingProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    attachment_id = create_confirmed_attachment(pipeline)

    async def scenario() -> None:
        task = asyncio.create_task(
            pipeline.consult(
                message="房东不退押金",
                attachment_ids=[attachment_id],
            )
        )
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())

    record = pipeline.attachments.get(attachment_id)
    assert record.status == "confirmed"
    assert record.reservation_id is None
    assert record.turn_id is None


def test_binding_failure_rolls_back_session_turn_and_attachment(
    tmp_path: Path,
) -> None:
    pipeline, store = make_pipeline(tmp_path)
    session = store.create_session()
    attachment_id = create_confirmed_attachment(pipeline)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_attachment_binding
            BEFORE UPDATE OF status ON attachments
            WHEN NEW.status = 'bound'
            BEGIN
                SELECT RAISE(ABORT, 'injected binding failure');
            END
            """
        )

    with pytest.raises(DataIntegrityError) as exc_info:
        run(
            pipeline.consult(
                session_id=session.id,
                message=(
                    "押金2000元，房东扣2000元，没理由，"
                    "合同没写可以扣。"
                ),
                attachment_ids=[attachment_id],
            )
        )

    assert exc_info.value.code == "pipeline_integrity_failed"
    restored_session = store.require_session(session.id)
    restored_attachment = pipeline.attachments.get(attachment_id)
    assert restored_session.scenario_id is None
    assert restored_session.facts == {}
    assert restored_session.followup_round == 0
    assert store.list_turns(session.id) == []
    assert restored_attachment.status == "confirmed"
    assert restored_attachment.reservation_id is None
    assert restored_attachment.session_id is None
    assert restored_attachment.turn_id is None


def test_provider_failure_refunds_reserved_quota(tmp_path: Path) -> None:
    provider = FakeProvider(
        error=ProviderError(
            "provider_timeout",
            "模型服务暂时不可用",
            retryable=True,
        )
    )
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    quota = QuotaService(InMemoryQuotaStore())
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id="11111111-1111-4111-8111-111111111111",
        logical_call_id=str(uuid4()),
    )

    result = run(
        pipeline.consult(
            message="房东不退押金",
            quota_call=controller,
        )
    )

    assert result.turn_kind == "unverified_guidance"
    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    assert quota.registered_status(
        "11111111-1111-4111-8111-111111111111"
    ).remaining_daily == 10


def test_provider_output_error_degrades_for_explicit_unverified_topic(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(error=ProviderOutputError())
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    quota = QuotaService(InMemoryQuotaStore())
    subject_id = "33333333-3333-4333-8333-333333333333"
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id=subject_id,
        logical_call_id=str(uuid4()),
    )

    result = run(
        pipeline.consult(
            message="老师打骂学生，学校一直不处理",
            quota_call=controller,
        )
    )

    assert result.turn_kind == "unverified_guidance"
    assert result.guidance is not None
    assert "采取保护措施" in result.guidance.direct_answer
    assert "教育主管部门" in result.guidance.direct_answer
    assert result.guidance.next_question == (
        "孩子目前是否仍处在可能继续受到伤害的环境中？"
    )
    assert result.coverage is not None
    assert result.coverage.topic_id == "education_minor_safety"
    assert result.coverage.playbook_id is None
    assert provider.extraction_calls == 1
    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    assert quota.registered_status(subject_id).remaining_daily == 10
    extraction_audit = next(
        record
        for record in store.list_audit_records(audit_id=result.audit_id)
        if record.stage == "extraction"
    )
    assert extraction_audit.status == "degraded"
    assert extraction_audit.error_category == "provider_invalid_output"


def test_provider_output_error_still_answers_privacy_delete_question(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(error=ProviderOutputError())
    pipeline, _ = make_pipeline(tmp_path, provider=provider)

    result = run(
        pipeline.consult(
            message=(
                "对方在网上公开我的姓名电话并造谣，"
                "我先要求删除可以吗？"
            ),
        )
    )

    assert result.turn_kind == "unverified_guidance"
    assert result.coverage is not None
    assert result.coverage.topic_id == "privacy_reputation"
    assert result.guidance is not None
    assert "可以先要求删除" in result.guidance.direct_answer
    assert "先完整保存" in result.guidance.direct_answer
    assert "向平台举报" in result.guidance.direct_answer
    assert result.reply_citations
    assert all(
        citation.basis_scope == "general"
        for citation in result.reply_citations
    )
    assert provider.extraction_calls == 1


@pytest.mark.parametrize(
    ("message", "expected_question"),
    [
        (
            "这是一件完全无法归类的事情",
            "请说明争议对象是谁、对方具体做了什么，以及你现在最希望解决的问题。",
        ),
        ("房东扣押金", "押金总额是多少元？"),
    ],
)
def test_provider_output_error_without_unverified_alias_refunds_quota(
    tmp_path: Path,
    message: str,
    expected_question: str,
) -> None:
    provider = FakeProvider(error=ProviderOutputError())
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    quota = QuotaService(InMemoryQuotaStore())
    subject_id = "44444444-4444-4444-8444-444444444444"
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id=subject_id,
        logical_call_id=str(uuid4()),
    )

    result = run(
        pipeline.consult(
            message=message,
            quota_call=controller,
        )
    )

    assert result.turn_kind == "unverified_guidance"
    assert result.guidance is not None
    assert result.guidance.direct_answer
    assert result.guidance.next_question == expected_question
    assert result.coverage is not None
    assert result.coverage.mode == "unverified_guidance"
    assert result.coverage.topic_id == "unknown"
    assert result.scenario_id is None
    assert store.require_session(result.session_id).scenario_id is None
    assert result.provider_request_id is None
    assert provider.extraction_calls == 1
    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    assert quota.registered_status(subject_id).remaining_daily == 10


def test_recovered_provider_confirms_degraded_formal_topic_from_context(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        error=ProviderError(
            "provider_timeout",
            retryable=True,
        )
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)

    first = run(pipeline.consult(message="房东不退押金"))
    degraded_session = store.require_session(first.session_id)
    provider._error = None  # noqa: SLF001 - simulate provider recovery

    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="2000元",
        )
    )
    recovered_session = store.require_session(first.session_id)

    assert first.guidance is not None
    assert first.guidance.next_question == "押金总额是多少元？"
    assert degraded_session.scenario_id is None
    assert second.coverage is not None
    assert second.coverage.mode == "formal"
    assert second.coverage.topic_id == "deposit_deduction"
    assert second.scenario_id == "deposit_deduction"
    assert second.questions == ("房东准备扣减或拒绝返还多少元？",)
    assert recovered_session.scenario_id == "deposit_deduction"
    assert recovered_session.facts["deposit_amount"] == 2000.0
    assert provider.extraction_calls == 2
    assert (
        provider.extraction_context_calls[-1]["previous_topic_id"]
        == "deposit_deduction"
    )


def test_repeated_provider_failure_keeps_degraded_formal_question_relevant(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        error=ProviderError(
            "provider_timeout",
            retryable=True,
        )
    )
    pipeline, store = make_pipeline(tmp_path, provider=provider)

    first = run(pipeline.consult(message="房东不退押金"))
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="2000元",
        )
    )
    session = store.require_session(first.session_id)

    assert first.guidance is not None
    assert first.guidance.next_question == "押金总额是多少元？"
    assert second.guidance is not None
    assert second.guidance.next_question == (
        "房东准备扣减或拒绝返还多少元？"
    )
    assert second.coverage is not None
    assert second.coverage.mode == "unverified_guidance"
    assert second.coverage.topic_id == "unknown"
    assert session.scenario_id is None
    assert session.facts == {}
    assert provider.extraction_calls == 2


def test_provider_failure_in_formal_session_asks_next_unasked_fact(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)

    first = run(pipeline.consult(message="房东不退押金"))
    before = store.require_session(first.session_id)
    provider._error = ProviderError(  # noqa: SLF001 - injected fault
        "provider_network",
        retryable=True,
    )

    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="2000元",
        )
    )
    session = store.require_session(first.session_id)

    assert first.questions == ("押金总额是多少元？",)
    assert second.reply is not None
    assert "房东准备扣减或拒绝返还多少元？" in second.reply.text
    assert second.coverage is not None
    assert second.coverage.mode == "formal"
    assert second.coverage.topic_id == "deposit_deduction"
    assert second.provider_request_id is None
    assert session.scenario_id == "deposit_deduction"
    assert session.facts == before.facts
    assert provider.extraction_calls == 2


def test_provider_output_fallback_prefers_explicit_current_topic(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    first = run(
        pipeline.consult(
            message="医院不肯给我病历，医疗收费也说不清",
        )
    )
    provider._error = ProviderOutputError()  # noqa: SLF001 - injected fault

    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="另外一件事，房东一直不维修，我想提前退租",
        )
    )

    assert first.coverage is not None
    assert first.coverage.topic_id == "medical_service_dispute"
    assert second.turn_kind == "unverified_guidance"
    assert second.coverage is not None
    assert second.coverage.topic_id == "general_rental"
    assert second.coverage.topic_label != first.coverage.topic_label
    assert second.guidance is not None
    assert second.guidance.direct_answer is not None
    assert "房东" in second.guidance.direct_answer
    assert len(store.list_turns(first.session_id)) == 2


def test_first_emergency_survives_invalid_provider_output_and_refunds_quota(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(error=ProviderOutputError())
    pipeline, store = make_pipeline(tmp_path, provider=provider)
    quota = QuotaService(InMemoryQuotaStore())
    subject_id = "55555555-5555-4555-8555-555555555555"
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id=subject_id,
        logical_call_id=str(uuid4()),
    )

    result = run(
        pipeline.consult(
            message="孩子正在被打，未成年人正在受伤害",
            quota_call=controller,
        )
    )

    assert result.turn_kind == "emergency_guidance"
    assert result.status == "escalate"
    assert result.coverage is not None
    assert "minor_harm" in result.coverage.risk_flags
    assert result.guidance is not None
    assert result.provider_request_id is None
    assert result.usage.total_tokens == 0
    assert provider.extraction_calls == 1
    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    assert quota.registered_status(subject_id).remaining_daily == 10
    extraction_audit = next(
        record
        for record in store.list_audit_records(audit_id=result.audit_id)
        if record.stage == "extraction"
    )
    assert extraction_audit.status == "degraded"
    assert extraction_audit.error_category == "provider_invalid_output"


def test_repeated_provider_output_clarification_is_persisted(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(error=ProviderOutputError())
    pipeline, store = make_pipeline(tmp_path, provider=provider)

    first = run(
        pipeline.consult(message="这是一件完全无法归类的事情")
    )
    second = run(
        pipeline.consult(
            session_id=first.session_id,
            message="这是一件完全无法归类的事情",
        )
    )

    assert first.guidance is not None
    assert second.guidance is not None
    assert first.guidance.next_question is not None
    assert second.guidance.next_question is None
    assert second.turn_kind == "unverified_guidance"
    assert provider.extraction_calls == 2
    assert len(store.list_turns(first.session_id)) == 2


def test_provider_context_excludes_other_users_conversation(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    pipeline, _ = make_pipeline(tmp_path, provider=provider)
    owner_a = str(uuid4())
    owner_b = str(uuid4())
    private_marker = "甲用户专属隐私标记ZXCV-814"
    run(
        pipeline.consult(
            message=f"医院不肯给病历，{private_marker}",
            owner_id=owner_a,
        )
    )
    second_user_message = (
        "我把游戏账号借给网友，他开挂导致账号被封了十年"
    )
    second_user = run(
        pipeline.consult(
            message=second_user_message,
            owner_id=owner_b,
        )
    )

    run(
        pipeline.consult(
            session_id=second_user.session_id,
            message="对方把我删除了，我接下来该怎么办？",
            owner_id=owner_b,
        )
    )

    context = provider.extraction_context_calls[-1]
    serialized_context = json.dumps(context, ensure_ascii=False)
    assert private_marker not in serialized_context
    assert second_user_message in serialized_context


def test_persistence_failure_after_provider_success_refunds_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, store = make_pipeline(tmp_path)
    quota = QuotaService(InMemoryQuotaStore())
    subject_id = "22222222-2222-4222-8222-222222222222"
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id=subject_id,
        logical_call_id=str(uuid4()),
    )

    def fail_persistence(*args, **kwargs):
        del args, kwargs
        raise OSError("injected persistence failure")

    monkeypatch.setattr(store, "persist_session_turn", fail_persistence)

    with pytest.raises(StorageUnavailableError):
        run(
            pipeline.consult(
                message="房东不退押金",
                quota_call=controller,
            )
        )

    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    assert quota.registered_status(subject_id).remaining_daily == 10


@pytest.mark.parametrize(
    "persistence_error",
    [CaseNoProgressError(), ConsultationConflictError()],
)
def test_persistence_recheck_rejection_refunds_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistence_error: Exception,
) -> None:
    pipeline, store = make_pipeline(tmp_path)
    quota = QuotaService(InMemoryQuotaStore())
    subject_id = "66666666-6666-4666-8666-666666666666"
    controller = QuotaCallController(
        quota,
        kind="registered",
        subject_id=subject_id,
        logical_call_id=str(uuid4()),
    )

    def reject_persistence(*args, **kwargs):
        del args, kwargs
        raise persistence_error

    monkeypatch.setattr(
        store,
        "persist_session_turn",
        reject_persistence,
    )

    with pytest.raises(type(persistence_error)):
        run(
            pipeline.consult(
                message="房东不退押金",
                quota_call=controller,
            )
        )

    assert controller.reservation_id is not None
    assert quota.store.get_reservation(
        controller.reservation_id
    ).status == "refunded"
    assert quota.registered_status(subject_id).remaining_daily == 10
