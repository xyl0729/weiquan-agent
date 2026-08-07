from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any, Literal
from uuid import UUID, uuid4

from app.agent.errors import (
    DataIntegrityError,
    ProviderError,
    ProviderOutputError,
    RequestInputError,
    SafeApplicationError,
    StorageUnavailableError,
)
from app.agent.continuation import (
    build_case_continuation_context,
    has_historical_plan,
)
from app.agent.models import (
    CaseContinuationContext,
    CaseContinuationResult,
    ExtractionResult,
    TurnKind,
    UsageInfo,
)
from app.attachments.context import EvidenceContextBuilder
from app.attachments.errors import AttachmentResourceLimitError
from app.attachments.models import (
    AttachmentEvidenceContext,
    AttachmentTurnPublic,
)
from app.attachments.store import AttachmentStore
from app.config import Settings
from app.db.models import AttachmentRecord, SessionRecord, TurnRecord
from app.db.session import SessionStore
from app.jurisdiction.rules import (
    JurisdictionRegistry,
    evaluate_jurisdiction,
)
from app.limits.usage import ProviderUsageControls
from app.playbooks.evaluator import (
    EvaluationResult,
    evaluate_playbook,
    matching_escalations,
)
from app.playbooks.registry import PlaybookRegistry
from app.playbooks.schema import Playbook
from app.providers.base import LLMProvider
from app.rendering.models import (
    LegalCitation,
    LockedConsultationDraft,
    RenderedDocuments,
    build_consultation_draft,
)
from app.rendering.renderer import PlanRenderer
from app.retrieval.database import (
    StatuteRecord,
    connect_database,
    get_metadata,
    get_statute_by_ref,
)


PipelineStatus = Literal["need_more_facts", "ready", "escalate"]

PIPELINE_STAGES = (
    "request_validation",
    "session",
    "playbook_registry",
    "extraction",
    "facts_validation",
    "followup",
    "retrieval",
    "rules",
    "jurisdiction",
    "draft",
    "rendering",
    "persistence",
    "response",
)


@dataclass(frozen=True, slots=True)
class PipelineReply:
    text: str
    suggested_actions: tuple[str, ...] = ()
    citation_refs: tuple[str, ...] = ()
    new_case: dict[str, str | None] | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "suggested_actions": list(self.suggested_actions),
            "citation_refs": list(self.citation_refs),
            "new_case": self.new_case,
        }


@dataclass(frozen=True, slots=True)
class PipelineResult:
    session_id: str
    turn_id: str
    audit_id: str
    followup_round: int
    can_ask_more: bool
    status: PipelineStatus
    turn_kind: TurnKind
    questions: tuple[str, ...]
    limitations: tuple[str, ...]
    provider_name: str
    provider_model: str
    provider_request_id: str | None
    usage: UsageInfo
    scenario_id: str | None = None
    playbook_version: str | None = None
    draft: LockedConsultationDraft | None = None
    rendered: RenderedDocuments | None = None
    reply: PipelineReply | None = None
    reply_citations: tuple[LegalCitation, ...] = ()
    attachments: tuple[AttachmentTurnPublic, ...] = ()

    def public_payload(self) -> dict[str, Any]:
        verdict: dict[str, Any] | None = None
        plan: dict[str, Any] | None = None
        citations: list[dict[str, Any]] = []
        if self.draft is not None:
            structured = self.draft.plan
            verdict = structured.verdict.model_dump(mode="json")
            citations = [
                citation.model_dump(mode="json")
                for citation in structured.citations
            ]
            jurisdiction = structured.jurisdiction.model_dump(mode="json")
            jurisdiction["notices"] = jurisdiction.pop("messages")
            plan = {
                "summary": structured.summary,
                "evidence_now": [
                    item.text
                    for item in structured.evidence_now
                ],
                "actions": [item.text for item in structured.actions],
                "communication_text": structured.communication_text,
                "limitations": structured.limitations,
                "time_limit": (
                    structured.time_limit.model_dump(mode="json")
                    if structured.time_limit is not None
                    else None
                ),
                "jurisdiction": jurisdiction,
                "rendered_text": (
                    self.rendered.plan_text
                    if self.rendered is not None
                    else None
                ),
                "evidence_request_text": (
                    self.rendered.evidence_request_text
                    if self.rendered is not None
                    else None
                ),
            }
        elif self.reply_citations:
            citations = [
                citation.model_dump(mode="json")
                for citation in self.reply_citations
            ]
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "audit_id": self.audit_id,
            "followup_round": self.followup_round,
            "can_ask_more": self.can_ask_more,
            "status": self.status,
            "turn_kind": self.turn_kind,
            "verdict": verdict,
            "plan": plan,
            "reply": (
                self.reply.public_payload()
                if self.reply is not None
                else None
            ),
            "questions": list(self.questions),
            "limitations": list(self.limitations),
            "citations": citations,
            "attachments": [
                attachment.model_dump(mode="json")
                for attachment in self.attachments
            ],
            "usage": {
                "provider": self.provider_name,
                "model": self.provider_model,
                "request_id": self.provider_request_id,
                **self.usage.model_dump(mode="json"),
            },
        }


@dataclass(frozen=True, slots=True)
class _PlanArtifacts:
    statutes: tuple[StatuteRecord, ...]
    evaluation: EvaluationResult
    draft: LockedConsultationDraft
    rendered: RenderedDocuments
    status: PipelineStatus


@dataclass(frozen=True, slots=True)
class _AuditEvent:
    stage: str
    status: Literal["ok", "error", "degraded"]
    duration_ms: int
    citations: tuple[str, ...] = ()
    error_category: str | None = None


@dataclass(slots=True)
class _RunState:
    events: list[_AuditEvent] = field(default_factory=list)
    active_stage: str = "request_validation"
    stage_started: float = field(default_factory=perf_counter)
    session: SessionRecord | None = None
    playbook: Playbook | None = None
    attachment_ids: tuple[str, ...] = ()
    reservation_id: str | None = None

    def begin(self, stage: str) -> None:
        self.active_stage = stage
        self.stage_started = perf_counter()

    def finish(
        self,
        *,
        status: Literal["ok", "error", "degraded"] = "ok",
        citations: tuple[str, ...] = (),
        error_category: str | None = None,
    ) -> None:
        self.events.append(
            _AuditEvent(
                stage=self.active_stage,
                status=status,
                duration_ms=max(
                    0,
                    int((perf_counter() - self.stage_started) * 1000),
                ),
                citations=citations,
                error_category=error_category,
            )
        )


class ConsultationPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SessionStore,
        attachments: AttachmentStore,
        registry: PlaybookRegistry,
        provider: LLMProvider,
        jurisdictions: JurisdictionRegistry,
        renderer: PlanRenderer,
        usage_controls: ProviderUsageControls | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.attachments = attachments
        self.evidence_context = EvidenceContextBuilder(
            attachments,
            max_attachments=settings.max_attachments_per_turn,
            max_context_chars=settings.max_attachment_context_chars,
        )
        self.registry = registry
        self.provider = provider
        self.jurisdictions = jurisdictions
        self.renderer = renderer
        self.usage_controls = usage_controls

    async def consult(
        self,
        *,
        message: str,
        session_id: str | None = None,
        jurisdiction: str | None = None,
        client_identifier: str = "local",
        attachment_ids: Sequence[str] = (),
    ) -> PipelineResult:
        run = _RunState()
        audit_id = str(uuid4())
        turn_id = str(uuid4())
        extraction: ExtractionResult | None = None
        evidence: tuple[AttachmentEvidenceContext, ...] = ()
        turns: list[TurnRecord] = []
        try:
            normalized_message = self._validate_message(message)
            normalized_jurisdiction = _optional_text(jurisdiction)
            run.attachment_ids = _attachment_ids(
                attachment_ids,
                max_attachments=self.settings.max_attachments_per_turn,
            )
            if run.attachment_ids:
                run.reservation_id = self.attachments.reserve(
                    run.attachment_ids
                )
                evidence = self.evidence_context.build(
                    run.attachment_ids,
                    reservation_id=run.reservation_id,
                )
            run.finish()

            run.begin("session")
            if session_id is None:
                run.session = self.store.create_session(
                    jurisdiction=normalized_jurisdiction
                )
            else:
                run.session = self.store.require_session(session_id)
                turns = self.store.list_turns(run.session.id)
            active_jurisdiction = (
                normalized_jurisdiction
                if normalized_jurisdiction is not None
                else run.session.jurisdiction
            )
            run.finish()

            run.begin("playbook_registry")
            if run.session.scenario_id is not None:
                try:
                    run.playbook = self.registry.get(
                        run.session.scenario_id
                    )
                except LookupError as exc:
                    raise DataIntegrityError(
                        "session_playbook_missing",
                        "会话对应的场景规则不存在",
                    ) from exc
            context = self.registry.provider_context(
                scenario_id=(
                    run.playbook.id
                    if run.playbook is not None
                    else None
                ),
                existing_facts=run.session.facts,
            )
            run.finish()

            if (
                run.playbook is not None
                and has_historical_plan(turns)
            ):
                return await self._continue_existing_case(
                    run=run,
                    turns=turns,
                    message=normalized_message,
                    active_jurisdiction=active_jurisdiction,
                    client_identifier=client_identifier,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    evidence=evidence,
                )

            run.begin("extraction")
            if self.usage_controls is not None:
                self.usage_controls.before_call(client_identifier)
            extraction = await self.provider.extract_facts(
                normalized_message,
                context,
                evidence,
            )
            self._verify_provider_identity(extraction)
            if self.usage_controls is not None:
                extraction = extraction.model_copy(
                    update={
                        "usage": self.usage_controls.after_call(
                            client_identifier,
                            extraction.usage,
                        )
                    }
                )
            run.finish()

            run.begin("facts_validation")
            run.playbook = self._resolve_playbook(
                extraction,
                current=run.playbook,
            )
            if run.playbook is None:
                facts = dict(run.session.facts)
                missing_required: list[str] = []
            else:
                facts, missing_required = self._merge_facts(
                    run.playbook,
                    run.session.facts,
                    extraction,
                )
            run.finish()

            run.begin("followup")
            if run.playbook is None:
                result = self._followup_result(
                    session=run.session,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    extraction=extraction,
                    scenario_id=None,
                    facts=facts,
                    questions=[
                        "请说明纠纷属于租赁、消费、劳动、培训、"
                        "装修或诉讼程序中的哪一类，并补充关键金额和日期。"
                    ],
                )
                run.finish()
                return self._persist_success(
                    run,
                    result,
                    message=normalized_message,
                    facts=facts,
                    rule_matches=[],
                    jurisdiction=active_jurisdiction,
                )

            if missing_required:
                questions = [
                    run.playbook.slots.by_name[name].ask
                    for name in missing_required
                    if run.playbook.slots.by_name[name].ask
                ]
                result = self._followup_result(
                    session=run.session,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    extraction=extraction,
                    scenario_id=run.playbook.id,
                    facts=facts,
                    questions=[str(question) for question in questions],
                    playbook_version=run.playbook.version,
                )
                run.finish()
                return self._persist_success(
                    run,
                    result,
                    message=normalized_message,
                    facts=facts,
                    rule_matches=[],
                    jurisdiction=active_jurisdiction,
                )
            run.finish()

            artifacts = self._build_plan(
                run=run,
                playbook=run.playbook,
                facts=facts,
                active_jurisdiction=active_jurisdiction,
            )
            result = PipelineResult(
                session_id=run.session.id,
                turn_id=turn_id,
                audit_id=audit_id,
                followup_round=run.session.followup_round,
                can_ask_more=False,
                status=artifacts.status,
                turn_kind="initial_plan",
                questions=(),
                limitations=(),
                provider_name=extraction.provider,
                provider_model=extraction.model,
                provider_request_id=extraction.request_id,
                usage=extraction.usage,
                scenario_id=run.playbook.id,
                playbook_version=run.playbook.version,
                draft=artifacts.draft,
                rendered=artifacts.rendered,
            )
            return self._persist_success(
                run,
                result,
                message=normalized_message,
                facts=artifacts.evaluation.facts,
                rule_matches=[
                    match.model_dump(mode="json")
                    for match in artifacts.evaluation.matches
                ],
                jurisdiction=active_jurisdiction,
            )
        except SafeApplicationError as exc:
            self._persist_failure(run, audit_id, exc.code)
            raise
        except (sqlite3.Error, OSError) as exc:
            safe_error = StorageUnavailableError()
            self._persist_failure(run, audit_id, safe_error.code)
            raise safe_error from exc
        except Exception as exc:
            safe_error = DataIntegrityError(
                "pipeline_integrity_failed",
                "咨询流水线完整性检查失败",
            )
            self._persist_failure(run, audit_id, safe_error.code)
            raise safe_error from exc
        finally:
            if run.reservation_id is not None:
                self.attachments.release(run.reservation_id)

    async def _continue_existing_case(
        self,
        *,
        run: _RunState,
        turns: list[TurnRecord],
        message: str,
        active_jurisdiction: str | None,
        client_identifier: str,
        turn_id: str,
        audit_id: str,
        evidence: tuple[AttachmentEvidenceContext, ...],
    ) -> PipelineResult:
        if run.session is None or run.playbook is None:
            raise RuntimeError("案件续问缺少当前会话或场景")

        run.begin("retrieval")
        statutes = self._retrieve_statutes(run.playbook)
        citation_refs = tuple(statute.ref for statute in statutes)
        run.finish(citations=citation_refs)

        context = build_case_continuation_context(
            playbook=run.playbook,
            registry=self.registry,
            existing_facts=run.session.facts,
            statutes=statutes,
            turns=turns,
        )

        run.begin("case_continuation")
        if self.usage_controls is not None:
            self.usage_controls.before_call(client_identifier)
        continuation = await self.provider.continue_case(
            message,
            context,
            evidence,
        )
        self._verify_provider_identity(continuation)
        if self.usage_controls is not None:
            continuation = continuation.model_copy(
                update={
                    "usage": self.usage_controls.after_call(
                        client_identifier,
                        continuation.usage,
                    )
                }
            )
        run.finish()

        run.begin("facts_validation")
        self._validate_continuation_whitelists(
            run.playbook,
            context,
            continuation,
        )
        if continuation.route == "new_case":
            new_case = self._resolve_new_case(
                run.playbook,
                continuation,
            )
            run.finish()
            run.begin("followup")
            result = PipelineResult(
                session_id=run.session.id,
                turn_id=turn_id,
                audit_id=audit_id,
                followup_round=run.session.followup_round,
                can_ask_more=False,
                status=_session_pipeline_status(run.session),
                turn_kind="new_case",
                questions=(),
                limitations=(),
                provider_name=continuation.provider,
                provider_model=continuation.model,
                provider_request_id=continuation.request_id,
                usage=continuation.usage,
                scenario_id=run.playbook.id,
                playbook_version=run.playbook.version,
                reply=PipelineReply(
                    text=(
                        "这看起来是另一项纠纷，建议单独建立咨询，"
                        "避免两件事相互混淆。"
                    ),
                    new_case=new_case,
                ),
            )
            run.finish()
            return self._persist_success(
                run,
                result,
                message=message,
                facts=run.session.facts,
                rule_matches=[],
                jurisdiction=run.session.jurisdiction,
                bind_attachments=False,
            )

        if continuation.scenario_id != run.playbook.id:
            raise ProviderOutputError("续问结果与当前案件场景不一致")
        facts, facts_changed = self._apply_continuation_facts(
            run.playbook,
            run.session.facts,
            continuation,
        )
        run.finish()

        if facts_changed:
            artifacts = self._build_plan(
                run=run,
                playbook=run.playbook,
                facts=facts,
                active_jurisdiction=active_jurisdiction,
                statutes=statutes,
            )
            result = PipelineResult(
                session_id=run.session.id,
                turn_id=turn_id,
                audit_id=audit_id,
                followup_round=run.session.followup_round,
                can_ask_more=False,
                status=artifacts.status,
                turn_kind="plan_update",
                questions=(),
                limitations=(),
                provider_name=continuation.provider,
                provider_model=continuation.model,
                provider_request_id=continuation.request_id,
                usage=continuation.usage,
                scenario_id=run.playbook.id,
                playbook_version=run.playbook.version,
                draft=artifacts.draft,
                rendered=artifacts.rendered,
            )
            return self._persist_success(
                run,
                result,
                message=message,
                facts=artifacts.evaluation.facts,
                rule_matches=[
                    match.model_dump(mode="json")
                    for match in artifacts.evaluation.matches
                ],
                jurisdiction=active_jurisdiction,
            )

        run.begin("followup")
        reply, reply_citations = self._build_followup_reply(
            context,
            statutes,
            continuation,
        )
        result = PipelineResult(
            session_id=run.session.id,
            turn_id=turn_id,
            audit_id=audit_id,
            followup_round=run.session.followup_round,
            can_ask_more=False,
            status=_session_pipeline_status(run.session),
            turn_kind="followup_answer",
            questions=(),
            limitations=(),
            provider_name=continuation.provider,
            provider_model=continuation.model,
            provider_request_id=continuation.request_id,
            usage=continuation.usage,
            scenario_id=run.playbook.id,
            playbook_version=run.playbook.version,
            reply=reply,
            reply_citations=reply_citations,
        )
        run.finish(citations=reply.citation_refs)
        return self._persist_success(
            run,
            result,
            message=message,
            facts=run.session.facts,
            rule_matches=[],
            jurisdiction=active_jurisdiction,
        )

    def _build_plan(
        self,
        *,
        run: _RunState,
        playbook: Playbook,
        facts: dict[str, Any],
        active_jurisdiction: str | None,
        statutes: list[StatuteRecord] | None = None,
    ) -> _PlanArtifacts:
        if statutes is None:
            run.begin("retrieval")
            statutes = self._retrieve_statutes(playbook)
            run.finish(
                citations=tuple(statute.ref for statute in statutes)
            )
        citation_refs = tuple(statute.ref for statute in statutes)

        run.begin("rules")
        try:
            evaluation = evaluate_playbook(playbook, facts)
        except ValueError as exc:
            raise DataIntegrityError(
                "rule_evaluation_failed",
                "本地规则无法安全求值",
            ) from exc
        escalation_messages = matching_escalations(
            playbook,
            evaluation.facts,
        )
        run.finish()

        run.begin("jurisdiction")
        jurisdiction_outcome = evaluate_jurisdiction(
            playbook,
            self.jurisdictions,
            jurisdiction=active_jurisdiction,
            facts=evaluation.facts,
        )
        run.finish()

        run.begin("draft")
        try:
            draft = build_consultation_draft(
                playbook,
                evaluation,
                statutes,
                jurisdiction_outcome,
                additional_limitations=escalation_messages,
            )
        except (TypeError, ValueError) as exc:
            raise DataIntegrityError(
                "draft_integrity_failed",
                "结构化方案完整性检查失败",
            ) from exc
        run.finish(citations=citation_refs)

        run.begin("rendering")
        try:
            rendered = self.renderer.render(
                draft,
                plan_template=playbook.templates.plan,
                evidence_template=playbook.templates.evidence,
            )
        except (TypeError, ValueError, OSError) as exc:
            raise DataIntegrityError(
                "template_render_failed",
                "固定模板渲染失败",
            ) from exc
        run.finish(citations=citation_refs)

        status = _result_status(
            evaluation,
            jurisdiction_escalates=(
                jurisdiction_outcome.status == "escalate"
            ),
            playbook_escalates=bool(escalation_messages),
        )
        return _PlanArtifacts(
            statutes=tuple(statutes),
            evaluation=evaluation,
            draft=draft,
            rendered=rendered,
            status=status,
        )

    def _validate_continuation_whitelists(
        self,
        playbook: Playbook,
        context: CaseContinuationContext,
        continuation: CaseContinuationResult,
    ) -> None:
        allowed_slots = set(playbook.slot_names)
        if not set(continuation.facts).issubset(allowed_slots):
            raise ProviderOutputError("续问结果包含未声明的事实槽位")
        if not set(continuation.cleared_slots).issubset(allowed_slots):
            raise ProviderOutputError("续问结果包含未声明的撤回槽位")

        allowed_actions = {
            action.ref for action in context.locked_case.actions
        }
        if not set(continuation.action_refs).issubset(allowed_actions):
            raise ProviderOutputError("续问结果引用了未批准的行动")

        allowed_citations = {basis.ref for basis in playbook.legal_basis}
        if not set(continuation.citation_refs).issubset(
            allowed_citations
        ):
            raise ProviderOutputError("续问结果引用了当前场景以外的法条")

    def _resolve_new_case(
        self,
        current: Playbook,
        continuation: CaseContinuationResult,
    ) -> dict[str, str | None]:
        if continuation.scenario_id == "unsupported":
            if (
                continuation.confidence
                < self.settings.classification_min_confidence
            ):
                raise ProviderOutputError("新纠纷分类置信度不足")
            return {"scenario_id": None, "label": None}
        try:
            target = self.registry.get(continuation.scenario_id)
        except LookupError as exc:
            raise ProviderOutputError("续问结果包含未注册的新场景") from exc
        if target.id == current.id:
            raise ProviderOutputError("新纠纷不能与当前案件使用同一场景")
        return {"scenario_id": target.id, "label": target.name}

    def _apply_continuation_facts(
        self,
        playbook: Playbook,
        existing: dict[str, Any],
        continuation: CaseContinuationResult,
    ) -> tuple[dict[str, Any], bool]:
        try:
            normalized_existing = self.registry.validate_facts(
                playbook.id,
                existing,
                apply_defaults=False,
            ).facts
            normalized_updates = self.registry.validate_facts(
                playbook.id,
                continuation.facts,
                apply_defaults=False,
            ).facts
        except ValueError as exc:
            raise ProviderOutputError("续问结果中的事实槽位无效") from exc
        if set(normalized_updates) != set(continuation.facts):
            raise ProviderOutputError("续问事实必须提供明确的非空值")

        merged = dict(normalized_existing)
        for name in continuation.cleared_slots:
            merged.pop(name, None)
        merged.update(normalized_updates)
        try:
            validated = self.registry.validate_facts(
                playbook.id,
                merged,
                apply_defaults=False,
            )
        except ValueError as exc:
            raise ProviderOutputError("续问后的案件事实无效") from exc
        if validated.missing_required:
            raise ProviderOutputError("续问不能撤回形成方案所需的必填事实")
        return validated.facts, validated.facts != normalized_existing

    def _build_followup_reply(
        self,
        context: CaseContinuationContext,
        statutes: list[StatuteRecord],
        continuation: CaseContinuationResult,
    ) -> tuple[PipelineReply, tuple[LegalCitation, ...]]:
        answer = (continuation.answer or "").strip()
        if not answer:
            raise ProviderOutputError("同案续问缺少有效短回答")

        action_by_ref = {
            action.ref: action.text for action in context.locked_case.actions
        }
        statute_by_ref = {statute.ref: statute for statute in statutes}
        try:
            suggested_actions = tuple(
                action_by_ref[ref] for ref in continuation.action_refs
            )
            citations = tuple(
                LegalCitation.from_statute(statute_by_ref[ref])
                for ref in continuation.citation_refs
            )
        except KeyError as exc:
            raise ProviderOutputError("续问引用无法映射到已批准内容") from exc
        reply = PipelineReply(
            text=answer,
            suggested_actions=suggested_actions,
            citation_refs=tuple(
                citation.ref for citation in citations
            ),
        )
        return reply, citations

    def _validate_message(self, message: str) -> str:
        normalized = str(message).strip()
        if not normalized:
            raise RequestInputError("message 不能为空")
        if len(normalized) > self.settings.max_message_length:
            raise RequestInputError(
                f"message 长度不能超过 {self.settings.max_message_length}"
            )
        return normalized

    def _verify_provider_identity(
        self,
        result: ExtractionResult | CaseContinuationResult,
    ) -> None:
        if (
            result.provider != self.provider.name
            or result.model != self.provider.model
        ):
            raise ProviderOutputError("模型身份元数据无效")

    def _resolve_playbook(
        self,
        extraction: ExtractionResult,
        *,
        current: Playbook | None,
    ) -> Playbook | None:
        if current is not None:
            if extraction.scenario_id not in {
                current.id,
                "unsupported",
            }:
                raise ProviderOutputError("模型返回了会话之外的场景")
            return current

        if (
            extraction.scenario_id == "unsupported"
            or (
                extraction.confidence is not None
                and extraction.confidence
                < self.settings.classification_min_confidence
            )
        ):
            return None
        try:
            return self.registry.get(extraction.scenario_id)
        except LookupError as exc:
            raise ProviderOutputError("模型返回了未注册场景") from exc

    def _merge_facts(
        self,
        playbook: Playbook,
        existing: dict[str, Any],
        extraction: ExtractionResult,
    ) -> tuple[dict[str, Any], list[str]]:
        if not set(extraction.unknown_slots).issubset(
            playbook.required_slot_names
        ):
            raise ProviderOutputError("模型返回了未声明的未知槽位")
        try:
            new_facts = self.registry.validate_facts(
                playbook.id,
                extraction.facts,
                apply_defaults=False,
            ).facts
            merged = {**existing, **new_facts}
            validated = self.registry.validate_facts(
                playbook.id,
                merged,
                apply_defaults=False,
            )
        except ValueError as exc:
            raise ProviderOutputError("模型返回的事实槽位无效") from exc
        return validated.facts, validated.missing_required

    def _followup_result(
        self,
        *,
        session: SessionRecord,
        turn_id: str,
        audit_id: str,
        extraction: ExtractionResult,
        scenario_id: str | None,
        facts: dict[str, Any],
        questions: list[str],
        playbook_version: str | None = None,
    ) -> PipelineResult:
        del facts
        if session.followup_round < 2:
            followup_round = session.followup_round + 1
            selected_questions = tuple(questions)
            can_ask_more = followup_round < 2
            limitations: tuple[str, ...] = ()
        else:
            followup_round = 2
            selected_questions = ()
            can_ask_more = False
            limitations = (
                "已达到两轮追问上限，现有事实不足以形成确定性判断。",
            )
        return PipelineResult(
            session_id=session.id,
            turn_id=turn_id,
            audit_id=audit_id,
            followup_round=followup_round,
            can_ask_more=can_ask_more,
            status="need_more_facts",
            turn_kind="fact_collection",
            questions=selected_questions,
            limitations=limitations,
            provider_name=extraction.provider,
            provider_model=extraction.model,
            provider_request_id=extraction.request_id,
            usage=extraction.usage,
            scenario_id=scenario_id,
            playbook_version=playbook_version,
        )

    def _retrieve_statutes(
        self,
        playbook: Playbook,
    ) -> list[StatuteRecord]:
        try:
            connection = connect_database(
                self.settings.statute_database_path
            )
            try:
                metadata = get_metadata(connection)
                if metadata.get("placeholder_data") != "false":
                    raise DataIntegrityError(
                        "statute_database_unverified",
                        "本地法条库未通过核验门禁",
                    )
                if metadata.get("schema_version") != "1":
                    raise DataIntegrityError(
                        "statute_schema_mismatch",
                        "本地法条库版本不受支持",
                    )
                statutes: list[StatuteRecord] = []
                for basis in playbook.legal_basis:
                    statute = get_statute_by_ref(connection, basis.ref)
                    if statute is None:
                        raise DataIntegrityError(
                            "statute_reference_missing",
                            "方案所需法条引用缺失",
                        )
                    statutes.append(statute)
            finally:
                connection.close()
        except DataIntegrityError:
            raise
        except (FileNotFoundError, LookupError, sqlite3.Error) as exc:
            raise DataIntegrityError(
                "statute_retrieval_failed",
                "已核验法条检索失败",
            ) from exc
        return statutes

    def _persist_success(
        self,
        run: _RunState,
        result: PipelineResult,
        *,
        message: str,
        facts: dict[str, Any],
        rule_matches: list[dict[str, Any]],
        jurisdiction: str | None,
        bind_attachments: bool = True,
    ) -> PipelineResult:
        if run.session is None:
            raise RuntimeError("持久化前缺少会话")

        attachment_binder = None
        if bind_attachments and run.attachment_ids:
            if run.reservation_id is None:
                raise RuntimeError("附件绑定前缺少有效预留")
            attachment_binder = self.attachments.reservation_binder(
                run.reservation_id,
                expected_ids=run.attachment_ids,
            )

        run.begin("persistence")
        stored_result = replace(result, attachments=())
        self.store.persist_session_turn(
            run.session.id,
            scenario_id=result.scenario_id,
            facts=facts,
            followup_round=result.followup_round,
            status=result.status,
            jurisdiction=jurisdiction,
            turn_id=result.turn_id,
            user_message=message,
            rule_matches=rule_matches,
            response=stored_result.public_payload(),
            provider_name=result.provider_name,
            provider_model=result.provider_model,
            provider_request_id=result.provider_request_id,
            usage=result.usage,
            attachment_binder=attachment_binder,
        )
        run.finish()

        public_attachments: tuple[AttachmentTurnPublic, ...] = ()
        if attachment_binder is not None:
            public_attachments = tuple(
                _attachment_turn_public(record)
                for record in self.attachments.list_for_turn(
                    result.turn_id
                )
            )
        public_result = replace(
            result,
            attachments=public_attachments,
        )

        run.begin("response")
        run.finish()

        for event in run.events:
            self.store.add_audit_record(
                run.session.id,
                audit_id=result.audit_id,
                turn_id=result.turn_id,
                stage=event.stage,
                status=event.status,
                duration_ms=event.duration_ms,
                playbook_id=result.scenario_id,
                playbook_version=result.playbook_version,
                citations=event.citations,
                error_category=event.error_category,
            )
        return public_result

    def _persist_failure(
        self,
        run: _RunState,
        audit_id: str,
        error_category: str,
    ) -> None:
        if run.session is None:
            return
        if (
            not run.events
            or run.events[-1].stage != run.active_stage
            or run.events[-1].status == "ok"
        ):
            run.finish(
                status="error",
                error_category=error_category,
            )
        try:
            for event in run.events:
                self.store.add_audit_record(
                    run.session.id,
                    audit_id=audit_id,
                    stage=event.stage,
                    status=event.status,
                    duration_ms=event.duration_ms,
                    playbook_id=(
                        run.playbook.id
                        if run.playbook is not None
                        else None
                    ),
                    playbook_version=(
                        run.playbook.version
                        if run.playbook is not None
                        else None
                    ),
                    citations=event.citations,
                    error_category=event.error_category,
                )
        except (SafeApplicationError, sqlite3.Error, ValueError):
            return


def _result_status(
    evaluation: EvaluationResult,
    *,
    jurisdiction_escalates: bool,
    playbook_escalates: bool,
) -> PipelineStatus:
    if jurisdiction_escalates or playbook_escalates:
        return "escalate"
    if evaluation.status == "ready":
        return "ready"
    if evaluation.status == "escalate":
        return "escalate"
    return "need_more_facts"


def _session_pipeline_status(session: SessionRecord) -> PipelineStatus:
    if session.status == "ready":
        return "ready"
    if session.status == "escalate":
        return "escalate"
    return "need_more_facts"


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 100:
        raise RequestInputError("jurisdiction 长度不能超过 100")
    return normalized


def _attachment_ids(
    values: Sequence[str],
    *,
    max_attachments: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise RequestInputError("附件 ID 必须是列表")
    if len(values) > max_attachments:
        raise AttachmentResourceLimitError(
            "attachment_count_exceeded"
        )
    try:
        normalized = tuple(str(UUID(str(value))) for value in values)
    except (ValueError, AttributeError, TypeError) as exc:
        raise RequestInputError("附件 ID 必须是有效 UUID") from exc
    if len(normalized) != len(set(normalized)):
        raise RequestInputError("附件 ID 不得重复")
    return normalized


def _attachment_turn_public(
    record: AttachmentRecord,
) -> AttachmentTurnPublic:
    if (
        record.status != "bound"
        or record.page_count is None
        or record.extraction_method is None
        or record.confirmed_text is None
    ):
        raise DataIntegrityError(
            "attachment_projection_invalid",
            "附件公开信息完整性检查失败",
        )
    return AttachmentTurnPublic(
        id=record.id,
        original_name=record.original_name,
        media_type=record.media_type,
        size_bytes=record.size_bytes,
        page_count=record.page_count,
        extraction_method=record.extraction_method,
        warnings=record.warnings,
        confirmed_text=record.confirmed_text,
    )
