from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic, perf_counter
from typing import Any, Literal
from uuid import UUID, uuid4

from app.agent.errors import (
    CaseNoProgressError,
    DataIntegrityError,
    InvalidProviderError,
    ProviderError,
    ProviderOutputError,
    RequestInputError,
    SafeApplicationError,
    StorageUnavailableError,
)
from app.agent.progression import (
    classify_turn_intent,
    comparison_units,
    derive_unverified_stage,
    find_duplicate,
    has_emergency_resolution,
    has_unverified_stage_signal,
    is_continuation_message,
    is_direct_question,
    latest_coverage,
    limitation_already_served,
    more_precise_question,
    next_emergency_question,
    next_unfinished_action,
    normalize_visible_text,
    project_response,
    project_turns,
    questions_asked,
    requires_direct_answer,
    requested_unverified_stage,
)
from app.agent.grounding import (
    GroundedAnswerDraft,
    GroundingPacket,
    GroundingStatute,
    build_local_answer,
    general_basis_refs,
    merge_grounded_answer,
    should_compose_grounded_answer,
)
from app.agent.continuation import (
    build_case_continuation_context,
    has_historical_plan,
)
from app.agent.guidance import GuidanceBuilder
from app.agent.models import (
    CaseContinuationContext,
    CaseContinuationResult,
    CommunicationGuide,
    CoverageResult,
    ExtractionResult,
    GuidanceResult,
    RoutedExtraction,
    TurnKind,
    UsageInfo,
)
from app.agent.routing import (
    GENERIC_FACT_NAMES,
    ScenarioRouter,
    TopicDefinition,
    TopicRegistry,
)
from app.attachments.context import EvidenceContextBuilder
from app.attachments.errors import AttachmentResourceLimitError
from app.attachments.models import (
    AttachmentEvidenceContext,
    AttachmentTurnPublic,
)
from app.attachments.projection import attachment_turn_public
from app.auth.errors import RegistrationRequiredError
from app.config import Settings
from app.db.contracts import (
    AttachmentBindingCommand,
    ConsultationCommitCommand,
    ConversationRepository,
    SessionUpdateCommand,
    TurnWriteCommand,
    LOCAL_DEVELOPMENT_OWNER_ID,
)
from app.db.models import SessionRecord, TurnRecord
from app.db.contracts import (
    AttachmentRepository,
    ConsultationRepository,
)
from app.jurisdiction.rules import (
    JurisdictionRegistry,
    evaluate_jurisdiction,
)
from app.limits.usage import ProviderUsageControls
from app.limits.reservations import QuotaCallController
from app.playbooks.evaluator import (
    EvaluationResult,
    evaluate_playbook,
    matching_escalations,
)
from app.playbooks.registry import PlaybookRegistry
from app.playbooks.schema import Playbook
from app.providers.base import LLMProvider
from app.providers.catalog import ProviderResolver
from app.rendering.models import (
    ActionStep,
    EvidenceItem,
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
from app.retrieval.expansion import infer_topic


PipelineStatus = Literal["need_more_facts", "ready", "escalate"]

_RECENT_CONVERSATION_TURNS = 6
_RECENT_USER_MESSAGE_CHARS = 1200
_RECENT_ASSISTANT_REPLY_CHARS = 1600
_PROVIDER_OUTPUT_CLARIFICATION = (
    "请说明争议对象是谁、对方具体做了什么，以及你现在最希望解决的问题。"
)

PIPELINE_STAGES = (
    "request_validation",
    "session",
    "playbook_registry",
    "safety",
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
    coverage: CoverageResult | None = None
    guidance: GuidanceResult | None = None
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
                "communication_guide": (
                    structured.communication_guide.model_dump(mode="json")
                ),
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
            "coverage": (
                self.coverage.model_dump(mode="json")
                if self.coverage is not None
                else None
            ),
            "guidance": (
                self.guidance.model_dump(mode="json")
                if self.guidance is not None
                else None
            ),
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
    persist: bool = True
    transient_store: ConversationRepository | None = None
    quota_call: QuotaCallController | None = None
    turns: tuple[TurnRecord, ...] = ()
    provider_deadline: float | None = None
    provider_degraded: bool = False

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
        store: ConsultationRepository,
        attachments: AttachmentRepository,
        registry: PlaybookRegistry,
        provider: LLMProvider | None = None,
        provider_resolver: ProviderResolver | None = None,
        jurisdictions: JurisdictionRegistry,
        renderer: PlanRenderer,
        usage_controls: (
            ProviderUsageControls
            | Mapping[str, ProviderUsageControls]
            | None
        ) = None,
    ) -> None:
        if provider is None and provider_resolver is None:
            raise ValueError("必须配置 Provider 或 ProviderResolver")
        self.settings = settings
        self.store = store
        self.attachments = attachments
        self.evidence_context = EvidenceContextBuilder(
            attachments,
            max_attachments=settings.max_attachments_per_turn,
            max_context_chars=settings.max_attachment_context_chars,
        )
        self.registry = registry
        self.topics = TopicRegistry.from_playbooks(registry)
        self.router = ScenarioRouter(
            self.topics,
            min_confidence=settings.classification_min_confidence,
        )
        self.guidance_builder = GuidanceBuilder()
        self.provider_resolver = provider_resolver
        self.provider = (
            provider
            if provider is not None
            else provider_resolver.resolve()
        )
        self.jurisdictions = jurisdictions
        self.renderer = renderer
        if isinstance(usage_controls, Mapping):
            self.usage_controls_by_provider = dict(usage_controls)
            self.usage_controls = self.usage_controls_by_provider.get(
                settings.llm_provider
            )
        else:
            self.usage_controls = usage_controls
            self.usage_controls_by_provider = (
                {usage_controls.provider: usage_controls}
                if usage_controls is not None
                else {}
            )

    async def consult(
        self,
        *,
        message: str,
        session_id: str | None = None,
        jurisdiction: str | None = None,
        client_identifier: str = "local",
        attachment_ids: Sequence[str] = (),
        provider_id: str | None = None,
        owner_id: str | None = None,
        persist: bool = True,
        transient_store: ConversationRepository | None = None,
        quota_call: QuotaCallController | None = None,
    ) -> PipelineResult:
        normalized_owner_id = self._owner_id(owner_id)
        run = _RunState(
            persist=persist,
            transient_store=transient_store,
            quota_call=quota_call,
            provider_deadline=(
                monotonic() + self.settings.llm_total_timeout_seconds
            ),
        )
        audit_id = str(uuid4())
        turn_id = str(uuid4())
        extraction: ExtractionResult | None = None
        evidence: tuple[AttachmentEvidenceContext, ...] = ()
        turns: list[TurnRecord] = []
        normalized_message: str | None = None
        active_jurisdiction: str | None = None
        provider: LLMProvider | None = None
        try:
            normalized_message = self._validate_message(message)
            normalized_jurisdiction = _optional_text(jurisdiction)
            provider = self._resolve_provider(provider_id)
            usage_controls = self._usage_controls_for(provider)
            if (
                not persist
                and session_id is not None
                and transient_store is None
            ):
                raise RequestInputError("试用咨询不支持恢复会话")
            if not persist and attachment_ids:
                raise RequestInputError("试用咨询不支持附件")
            run.attachment_ids = _attachment_ids(
                attachment_ids,
                max_attachments=self.settings.max_attachments_per_turn,
            )
            if run.attachment_ids:
                run.reservation_id = self.attachments.reserve(
                    run.attachment_ids,
                    owner_id=normalized_owner_id,
                )
                evidence = self.evidence_context.build(
                    run.attachment_ids,
                    owner_id=normalized_owner_id,
                    reservation_id=run.reservation_id,
                )
            run.finish()

            run.begin("session")
            if not persist and transient_store is None:
                current = datetime.now(UTC)
                run.session = SessionRecord(
                    id=str(uuid4()),
                    owner_id=normalized_owner_id,
                    scenario_id=None,
                    facts={},
                    followup_round=0,
                    status="collecting",
                    jurisdiction=normalized_jurisdiction,
                    created_at=current,
                    updated_at=current,
                    expires_at=current + timedelta(hours=1),
                )
            elif not persist:
                if session_id is None:
                    run.session = transient_store.create_session(
                        owner_id=normalized_owner_id,
                        jurisdiction=normalized_jurisdiction,
                    )
                else:
                    run.session = transient_store.require_session(
                        session_id,
                        owner_id=normalized_owner_id,
                    )
                    turns = transient_store.list_turns(
                        run.session.id,
                        owner_id=normalized_owner_id,
                    )
            elif session_id is None:
                run.session = self.store.create_session(
                    owner_id=normalized_owner_id,
                    jurisdiction=normalized_jurisdiction
                )
            else:
                run.session = self.store.require_session(
                    session_id,
                    owner_id=normalized_owner_id,
                )
                turns = self.store.list_turns(
                    run.session.id,
                    owner_id=normalized_owner_id,
                )
            run.turns = tuple(turns)
            if run.session.owner_id != normalized_owner_id:
                raise DataIntegrityError(
                    "session_owner_mismatch",
                    "会话所有权完整性检查失败",
                )
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
            context = {
                **self.registry.provider_context(
                    scenario_id=(
                        run.playbook.id
                        if run.playbook is not None
                        else None
                    ),
                    existing_facts=run.session.facts,
                ),
                **self.topics.provider_context(),
            }
            historical_unverified = latest_coverage(
                turns,
                mode="unverified_guidance",
            )
            fallback_formal_topic = self._fallback_formal_topic(turns)
            conversation_topic = historical_unverified
            if fallback_formal_topic is not None:
                conversation_topic = {
                    "topic_id": fallback_formal_topic.id,
                    "topic_label": fallback_formal_topic.label,
                }
            previous_topic_id = (
                str(historical_unverified["topic_id"])
                if historical_unverified is not None
                else None
            )
            context.update(
                self._conversation_context(
                    turns=turns,
                    previous_topic=conversation_topic,
                    message=normalized_message,
                    existing_facts=run.session.facts,
                )
            )
            run.finish()

            run.begin("safety")
            emergency_coverage = self.router.emergency_coverage(
                normalized_message
            )
            if emergency_coverage is not None:
                run.finish()
                run.begin("extraction")
                extraction = await self._call_extraction(
                    run=run,
                    message=normalized_message,
                    context=context,
                    evidence=evidence,
                    client_identifier=client_identifier,
                    provider=provider,
                    usage_controls=usage_controls,
                )
                run.finish()
                run.begin("followup")
                result = self._emergency_result(
                    session=run.session,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    coverage=emergency_coverage,
                    facts=run.session.facts,
                    provider=provider,
                    turns=turns,
                    message=normalized_message,
                    extraction=extraction,
                )
                run.finish()
                return self._persist_success(
                    run,
                    result,
                    message=normalized_message,
                    facts=run.session.facts,
                    rule_matches=[],
                    jurisdiction=active_jurisdiction,
                    preserve_session_state=run.playbook is not None,
                )
            run.finish()

            if (
                turns
                and has_emergency_resolution(normalized_message)
                and self._has_emergency_interlude(turns)
            ):
                resumed = await self._resume_after_emergency(
                    run=run,
                    turns=turns,
                    message=normalized_message,
                    active_jurisdiction=active_jurisdiction,
                    provider=provider,
                    client_identifier=client_identifier,
                    usage_controls=usage_controls,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    evidence=evidence,
                    extraction_context=context,
                )
                if resumed is not None:
                    return resumed

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
                    provider=provider,
                    usage_controls=usage_controls,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    evidence=evidence,
                )

            if (
                historical_unverified is not None
                and fallback_formal_topic is None
                and (
                    is_continuation_message(normalized_message)
                    or self._is_unverified_status_update(
                        normalized_message
                    )
                )
            ):
                coverage = CoverageResult.model_validate(
                    historical_unverified
                )
                run.begin("extraction")
                extraction = await self._call_extraction(
                    run=run,
                    message=normalized_message,
                    context=context,
                    evidence=evidence,
                    client_identifier=client_identifier,
                    provider=provider,
                    usage_controls=usage_controls,
                )
                run.finish()
                run.begin("followup")
                result = await self._unverified_progress_result(
                    run=run,
                    session=run.session,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    coverage=coverage,
                    provider=provider,
                    turns=turns,
                    message=normalized_message,
                    client_identifier=client_identifier,
                    usage_controls=usage_controls,
                    extraction=extraction,
                )
                run.finish()
                return self._persist_success(
                    run,
                    result,
                    message=normalized_message,
                    facts=run.session.facts,
                    rule_matches=[],
                    jurisdiction=active_jurisdiction,
                    preserve_session_state=True,
                    allow_duplicate=(
                        extraction.turn_intent == "continue_case"
                    ),
                )

            run.begin("extraction")
            extraction = await self._call_extraction(
                run=run,
                message=normalized_message,
                context=context,
                evidence=evidence,
                client_identifier=client_identifier,
                provider=provider,
                usage_controls=usage_controls,
            )
            run.finish()

            run.begin("facts_validation")
            routed = self.router.route(
                extraction,
                message=normalized_message,
                previous_topic_id=previous_topic_id,
                contextual_formal_topic_id=(
                    fallback_formal_topic.id
                    if fallback_formal_topic is not None
                    else None
                ),
            )
            if (
                run.playbook is not None
                and routed.coverage.mode != "emergency_guidance"
            ):
                self._validate_current_playbook_candidate(
                    extraction,
                    run.playbook,
                )
                routed = RoutedExtraction(
                    coverage=self._formal_coverage(
                        run.playbook,
                        confidence=extraction.confidence,
                    ),
                    facts=extraction.facts,
                    unknown_slots=extraction.unknown_slots,
                )

            if routed.coverage.mode != "formal":
                facts = {
                    **self._guidance_facts(run.session.facts),
                    **routed.facts,
                }
                run.finish()
                run.begin("followup")
                if (
                    historical_unverified is not None
                    and routed.coverage.mode == "unverified_guidance"
                    and routed.coverage.topic_id == previous_topic_id
                ):
                    result = await self._unverified_progress_result(
                        run=run,
                        session=run.session,
                        turn_id=turn_id,
                        audit_id=audit_id,
                        coverage=routed.coverage,
                        provider=provider,
                        turns=turns,
                        message=normalized_message,
                        client_identifier=client_identifier,
                        usage_controls=usage_controls,
                        extraction=extraction,
                    )
                else:
                    result = await self._grounded_guidance_result(
                        run=run,
                        session=run.session,
                        turn_id=turn_id,
                        audit_id=audit_id,
                        coverage=routed.coverage,
                        facts=facts,
                        provider=provider,
                        turns=turns,
                        message=normalized_message,
                        client_identifier=client_identifier,
                        usage_controls=usage_controls,
                        extraction=extraction,
                    )
                run.finish()
                return self._persist_success(
                    run,
                    result,
                    message=normalized_message,
                    facts=facts,
                    rule_matches=[],
                    jurisdiction=active_jurisdiction,
                    preserve_session_state=run.playbook is not None,
                )

            if run.playbook is None:
                playbook_id = routed.coverage.playbook_id
                if playbook_id is None:
                    raise DataIntegrityError(
                        "formal_route_missing_playbook",
                        "正式路由缺少场景规则",
                    )
                try:
                    run.playbook = self.registry.get(playbook_id)
                except LookupError as exc:
                    raise DataIntegrityError(
                        "formal_route_playbook_missing",
                        "正式路由对应的场景规则不存在",
                    ) from exc

            facts, missing_required = self._merge_facts(
                run.playbook,
                run.session.facts,
                extraction,
            )
            coverage = self._formal_coverage(
                run.playbook,
                confidence=extraction.confidence,
            )
            run.finish()

            run.begin("followup")
            if missing_required:
                question_specs = [
                    (
                        str(slot.ask),
                        slot.type,
                    )
                    for name in missing_required
                    if (
                        (slot := run.playbook.slots.by_name[name]).ask
                        is not None
                    )
                ]
                result = await self._followup_result(
                    run=run,
                    session=run.session,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    extraction=extraction,
                    scenario_id=run.playbook.id,
                    facts=facts,
                    question_specs=question_specs,
                    turns=turns,
                    playbook_version=run.playbook.version,
                    coverage=coverage,
                    message=normalized_message,
                    playbook=run.playbook,
                    provider=provider,
                    usage_controls=usage_controls,
                    client_identifier=client_identifier,
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
            usage = extraction.usage
            provider_request_id = extraction.request_id
            if extraction.turn_intent in {"stated_goal", "correction"}:
                (
                    artifacts,
                    usage,
                    provider_request_id,
                ) = await self._ground_initial_plan(
                    run=run,
                    artifacts=artifacts,
                    coverage=coverage,
                    playbook=run.playbook,
                    turns=turns,
                    message=normalized_message,
                    extraction=extraction,
                    provider=provider,
                    usage_controls=usage_controls,
                    client_identifier=client_identifier,
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
                provider_request_id=provider_request_id,
                usage=usage,
                scenario_id=run.playbook.id,
                playbook_version=run.playbook.version,
                coverage=coverage,
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
        except ProviderError as exc:
            if (
                normalized_message is None
                or provider is None
                or run.session is None
            ):
                self._persist_failure(run, audit_id, exc.code)
                raise
            try:
                return self._persist_provider_fallback(
                    run=run,
                    message=normalized_message,
                    active_jurisdiction=active_jurisdiction,
                    provider=provider,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    error_category=exc.category,
                )
            except SafeApplicationError as fallback_exc:
                self._persist_failure(
                    run,
                    audit_id,
                    fallback_exc.code,
                )
                raise
            except sqlite3.IntegrityError as fallback_exc:
                safe_error = DataIntegrityError(
                    "pipeline_integrity_failed",
                    "咨询流水线完整性检查失败",
                )
                self._persist_failure(run, audit_id, safe_error.code)
                raise safe_error from fallback_exc
            except (sqlite3.Error, OSError) as fallback_exc:
                safe_error = StorageUnavailableError()
                self._persist_failure(run, audit_id, safe_error.code)
                raise safe_error from fallback_exc
            except Exception as fallback_exc:
                safe_error = DataIntegrityError(
                    "pipeline_integrity_failed",
                    "咨询流水线完整性检查失败",
                )
                self._persist_failure(run, audit_id, safe_error.code)
                raise safe_error from fallback_exc
        except SafeApplicationError as exc:
            self._persist_failure(run, audit_id, exc.code)
            raise
        except sqlite3.IntegrityError as exc:
            safe_error = DataIntegrityError(
                "pipeline_integrity_failed",
                "咨询流水线完整性检查失败",
            )
            self._persist_failure(run, audit_id, safe_error.code)
            raise safe_error from exc
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
            if run.quota_call is not None:
                try:
                    run.quota_call.refund()
                except Exception:
                    pass
            if run.reservation_id is not None:
                self.attachments.release(
                    run.reservation_id,
                    owner_id=normalized_owner_id,
                )

    def _persist_provider_fallback(
        self,
        *,
        run: _RunState,
        message: str,
        active_jurisdiction: str | None,
        provider: LLMProvider,
        turn_id: str,
        audit_id: str,
        error_category: str,
    ) -> PipelineResult:
        if run.session is None:
            raise RuntimeError("Provider 降级前缺少会话")

        run.finish(
            status="degraded",
            error_category=error_category,
        )
        emergency_coverage = self.router.emergency_coverage(message)
        run.begin("followup")
        if emergency_coverage is not None:
            result = self._emergency_result(
                session=run.session,
                turn_id=turn_id,
                audit_id=audit_id,
                coverage=emergency_coverage,
                facts=run.session.facts,
                provider=provider,
                turns=run.turns,
                message=message,
            )
            preserve_session_state = run.playbook is not None
        else:
            coverage = self._provider_fallback_coverage(
                run=run,
                message=message,
            )
            question = self._provider_fallback_question(
                run=run,
                coverage=coverage,
                message=message,
            )
            content_playbook = run.playbook
            if content_playbook is None:
                inferred = self._formal_topic_from_text(message)
                if inferred is not None and inferred.playbook_id is not None:
                    content_playbook = self.registry.get(
                        inferred.playbook_id
                    )
            if content_playbook is not None:
                actions = content_playbook.actions
                evidence_targets = content_playbook.evidence_now
                limitations = content_playbook.limitations
            else:
                fallback_guidance = self.guidance_builder.build(
                    coverage,
                    facts=self._guidance_facts(run.session.facts),
                    message=message,
                )
                actions = fallback_guidance.actions
                evidence_targets = fallback_guidance.evidence_now
                limitations = fallback_guidance.limitations

            try:
                if coverage.mode == "formal" and run.playbook is not None:
                    statutes = self._retrieve_statutes(run.playbook)[:3]
                    basis_scope: Literal["case_specific", "general"] = (
                        "case_specific"
                    )
                else:
                    statutes = self._retrieve_general_statutes(
                        topic_id=coverage.topic_id,
                        message=message,
                        turns=run.turns,
                    )
                    basis_scope = "general"
            except DataIntegrityError:
                statutes = []
                basis_scope = (
                    "case_specific"
                    if coverage.mode == "formal"
                    else "general"
                )
            packet = self._build_grounding_packet(
                message=message,
                coverage=coverage,
                facts=run.session.facts,
                turns=run.turns,
                actions=actions,
                evidence=evidence_targets,
                limitations=limitations,
                statutes=statutes,
                basis_scope=basis_scope,
                next_question=question,
                turn_intent=classify_turn_intent(message),
            )
            draft = build_local_answer(packet)
            citations = self._grounded_citations(packet, draft)
            if coverage.mode == "formal" and run.playbook is not None:
                result = PipelineResult(
                    session_id=run.session.id,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    followup_round=run.session.followup_round,
                    can_ask_more=draft.next_question is not None,
                    status=_session_pipeline_status(run.session),
                    turn_kind="followup_answer",
                    questions=(),
                    limitations=tuple(draft.limitations),
                    provider_name=provider.name,
                    provider_model=provider.model,
                    provider_request_id=None,
                    usage=UsageInfo(),
                    scenario_id=run.playbook.id,
                    playbook_version=run.playbook.version,
                    coverage=coverage,
                    reply=PipelineReply(
                        text=self._grounded_followup_text(draft),
                        suggested_actions=tuple(draft.actions[:3]),
                        citation_refs=tuple(
                            citation.ref for citation in citations
                        ),
                    ),
                    reply_citations=citations,
                )
            else:
                fallback_guidance = self.guidance_builder.build(
                    coverage,
                    facts=self._guidance_facts(run.session.facts),
                    message=message,
                )
                fallback_guidance = fallback_guidance.model_copy(
                    update={
                        "direct_answer": self._grounded_text(draft),
                        "actions": draft.actions,
                        "evidence_now": draft.evidence,
                        "limitations": draft.limitations,
                        "next_question": draft.next_question,
                    }
                )
                result = PipelineResult(
                    session_id=run.session.id,
                    turn_id=turn_id,
                    audit_id=audit_id,
                    followup_round=run.session.followup_round,
                    can_ask_more=draft.next_question is not None,
                    status="need_more_facts",
                    turn_kind="unverified_guidance",
                    questions=(),
                    limitations=tuple(draft.limitations),
                    provider_name=provider.name,
                    provider_model=provider.model,
                    provider_request_id=None,
                    usage=UsageInfo(),
                    scenario_id=None,
                    coverage=coverage,
                    guidance=fallback_guidance,
                    reply_citations=citations,
                )
            preserve_session_state = True
        run.finish()
        return self._persist_success(
            run,
            result,
            message=message,
            facts=dict(run.session.facts),
            rule_matches=[],
            jurisdiction=active_jurisdiction,
            bind_attachments=False,
            preserve_session_state=preserve_session_state,
            consume_quota=False,
            allow_duplicate=True,
        )

    def _provider_fallback_coverage(
        self,
        *,
        run: _RunState,
        message: str,
    ) -> CoverageResult:
        if (
            run.session is not None
            and run.session.scenario_id is not None
            and run.playbook is not None
            and run.playbook.id == run.session.scenario_id
        ):
            return self._formal_coverage(run.playbook)

        local_coverage = self.router.unverified_coverage(message)
        if local_coverage is not None:
            return local_coverage
        historical = latest_coverage(
            run.turns,
            mode="unverified_guidance",
        )
        if historical is not None:
            return CoverageResult.model_validate(historical)
        return CoverageResult(
            mode="unverified_guidance",
            topic_id="unknown",
            topic_label="其他待确认问题",
            confidence=None,
            playbook_id=None,
            notice=(
                "当前信息不足以可靠确定问题类型，"
                "先补充关键事实后再继续判断。"
            ),
            risk_flags=[],
        )

    def _provider_fallback_question(
        self,
        *,
        run: _RunState,
        coverage: CoverageResult,
        message: str,
    ) -> str:
        if run.session is None:
            raise RuntimeError("Provider 降级前缺少会话")
        if coverage.mode == "formal" and run.playbook is not None:
            question = self._next_unasked_missing_question(
                run.playbook,
                run.session.facts,
                turns=run.turns,
            )
            if question is not None:
                return question
            return (
                "请说明你想继续核对已有方案中的哪一个步骤，"
                "或当前最希望先解决的具体问题。"
            )
        if (
            coverage.mode == "unverified_guidance"
            and coverage.topic_id != "unknown"
        ):
            guidance = self.guidance_builder.build(
                coverage,
                facts=self._guidance_facts(run.session.facts),
                message=message,
            )
            if guidance.next_question is not None:
                return guidance.next_question
        formal_topic = self._formal_topic_from_text(message)
        if formal_topic is None:
            formal_topic = self._fallback_formal_topic(run.turns)
        if formal_topic is not None:
            playbook = self.registry.get(str(formal_topic.playbook_id))
            question = self._next_unasked_missing_question(
                playbook,
                run.session.facts,
                turns=run.turns,
            )
            if question is not None:
                return question
        return _PROVIDER_OUTPUT_CLARIFICATION

    def _fallback_formal_topic(
        self,
        turns: Sequence[TurnRecord],
    ) -> TopicDefinition | None:
        if not turns:
            return None
        turn = turns[-1]
        response = turn.response
        coverage = response.get("coverage")
        turn_kind = response.get("turn_kind")
        expected_question: str | None = None
        if turn_kind == "fact_collection":
            questions = response.get("questions")
            if (
                isinstance(questions, list)
                and len(questions) == 1
                and isinstance(questions[0], str)
            ):
                expected_question = questions[0]
        elif turn_kind == "unverified_guidance":
            guidance = response.get("guidance")
            if isinstance(guidance, Mapping):
                question = guidance.get("next_question")
                if isinstance(question, str):
                    expected_question = question
        if (
            turn.provider_request_id is not None
            or turn.usage.total_tokens != 0
            or not isinstance(coverage, Mapping)
            or coverage.get("mode") != "unverified_guidance"
            or coverage.get("topic_id") != "unknown"
            or expected_question is None
        ):
            return None
        topic = self._formal_topic_from_text(turn.user_message)
        if topic is None:
            return None
        playbook = self.registry.get(str(topic.playbook_id))
        expected = self._first_missing_question(playbook, turn.facts)
        if expected is None or expected_question != expected:
            return None
        return topic

    def _formal_topic_from_text(
        self,
        message: str,
    ) -> TopicDefinition | None:
        topic = self.topics.infer_from_text(message)
        if (
            topic is None
            or topic.coverage_mode != "formal"
            or topic.playbook_id is None
        ):
            return None
        return topic

    def _first_missing_question(
        self,
        playbook: Playbook,
        facts: Mapping[str, Any],
    ) -> str | None:
        compatible = {
            name: value
            for name, value in facts.items()
            if name in playbook.slots.by_name
        }
        try:
            validated = self.registry.validate_facts(
                playbook.id,
                compatible,
                apply_defaults=False,
            )
        except (TypeError, ValueError):
            validated = self.registry.validate_facts(
                playbook.id,
                {},
                apply_defaults=False,
            )
        for name in validated.missing_required:
            question = playbook.slots.by_name[name].ask
            if question is not None:
                return str(question)
        return None

    def _next_unasked_missing_question(
        self,
        playbook: Playbook,
        facts: Mapping[str, Any],
        *,
        turns: Sequence[TurnRecord],
    ) -> str | None:
        compatible = {
            name: value
            for name, value in facts.items()
            if name in playbook.slots.by_name
        }
        try:
            validated = self.registry.validate_facts(
                playbook.id,
                compatible,
                apply_defaults=False,
            )
        except (TypeError, ValueError):
            validated = self.registry.validate_facts(
                playbook.id,
                {},
                apply_defaults=False,
            )
        question_specs = [
            (str(slot.ask), slot.type)
            for name in validated.missing_required
            if (
                (slot := playbook.slots.by_name[name]).ask
                is not None
            )
        ]
        asked = set(questions_asked(turns))
        for question, _slot_type in question_specs:
            if normalize_visible_text(question) not in asked:
                return question
        for question, slot_type in question_specs:
            precise = more_precise_question(
                question,
                slot_type=slot_type,
            )
            if (
                precise
                and normalize_visible_text(precise) not in asked
            ):
                return precise
        return None

    def _owner_id(self, value: str | None) -> str:
        if value is None:
            if self.settings.deployment_mode != "local":
                raise RegistrationRequiredError()
            return LOCAL_DEVELOPMENT_OWNER_ID
        try:
            return str(UUID(str(value)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise RegistrationRequiredError() from exc

    async def _continue_existing_case(
        self,
        *,
        run: _RunState,
        turns: list[TurnRecord],
        message: str,
        active_jurisdiction: str | None,
        client_identifier: str,
        provider: LLMProvider,
        usage_controls: ProviderUsageControls | None,
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
        coverage = self._formal_coverage(run.playbook)

        run.begin("case_continuation")
        continuation = await self._call_continuation(
            run=run,
            message=message,
            context=context,
            evidence=evidence,
            client_identifier=client_identifier,
            provider=provider,
            usage_controls=usage_controls,
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
                coverage=coverage,
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
            next_action = next_unfinished_action(
                [
                    action.text
                    for action in context.locked_case.actions
                ],
                turns,
            )
            suggested_actions = (
                (next_action[1],)
                if next_action is not None
                else ()
            )
            change_summary = continuation.answer.strip()
            if continuation.cleared_slots:
                change_summary = (
                    f"{change_summary} 已按撤回后的事实重新核对本案方案。"
                )
            else:
                change_summary = (
                    f"{change_summary} 已按本轮补充的事实重新核对本案方案。"
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
                coverage=coverage,
                draft=artifacts.draft,
                rendered=artifacts.rendered,
                reply=PipelineReply(
                    text=change_summary,
                    suggested_actions=suggested_actions,
                ),
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
        action_by_ref = {
            action.ref: action.text
            for action in context.locked_case.actions
        }
        selected_actions = [
            action_by_ref[ref]
            for ref in continuation.action_refs
            if ref in action_by_ref
        ]
        if not selected_actions:
            next_action = next_unfinished_action(
                list(action_by_ref.values()),
                turns,
            )
            if next_action is not None:
                selected_actions = [next_action[1]]
        selected_refs = set(continuation.citation_refs)
        selected_statutes = [
            statute
            for statute in statutes
            if statute.ref in selected_refs
        ][:3]
        packet = self._build_grounding_packet(
            message=message,
            coverage=coverage,
            facts=run.session.facts,
            turns=turns,
            actions=selected_actions,
            evidence=context.locked_case.evidence,
            limitations=context.locked_case.limitations,
            statutes=selected_statutes,
            basis_scope="case_specific",
            direct_answer=continuation.answer,
            next_question=None,
            formal_findings=(
                context.locked_case.verdict_label,
                context.locked_case.key_point,
            ),
            turn_intent=classify_turn_intent(message),
        )
        draft = build_local_answer(packet)
        draft, usage, request_id = await self._compose_grounded_answer(
            run=run,
            packet=packet,
            draft=draft,
            is_followup=True,
            provider=provider,
            usage_controls=usage_controls,
            client_identifier=client_identifier,
            base_usage=continuation.usage,
            base_request_id=continuation.request_id,
        )
        reply_citations = self._grounded_citations(packet, draft)
        reply = PipelineReply(
            text=self._grounded_followup_text(draft),
            suggested_actions=tuple(draft.actions[:3]),
            citation_refs=tuple(
                citation.ref for citation in reply_citations
            ),
        )
        run.begin("followup")
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
            provider_request_id=request_id,
            usage=usage,
            scenario_id=run.playbook.id,
            playbook_version=run.playbook.version,
            coverage=coverage,
            reply=reply,
            reply_citations=reply_citations,
        )
        run.finish(citations=reply.citation_refs)
        consume_quota = True
        if find_duplicate(
            project_response(result.public_payload()),
            project_turns(turns),
        ).duplicate:
            result = self._next_formal_action_result(
                run=run,
                turns=turns,
                context=context,
                coverage=coverage,
                provider=provider,
                turn_id=turn_id,
                audit_id=audit_id,
                usage=usage,
                provider_request_id=request_id,
            )
            consume_quota = bool(
                result.reply is not None
                and result.reply.suggested_actions
            )
        return self._persist_success(
            run,
            result,
            message=message,
            facts=run.session.facts,
            rule_matches=[],
            jurisdiction=active_jurisdiction,
            consume_quota=consume_quota,
            allow_duplicate=(classify_turn_intent(message) == "continue_case"),
        )

    def _next_formal_action_result(
        self,
        *,
        run: _RunState,
        turns: Sequence[TurnRecord],
        context: CaseContinuationContext,
        coverage: CoverageResult,
        provider: LLMProvider,
        turn_id: str,
        audit_id: str,
        usage: UsageInfo | None = None,
        provider_request_id: str | None = None,
    ) -> PipelineResult:
        if run.session is None or run.playbook is None:
            raise RuntimeError("案件推进缺少当前会话或场景")
        next_action = next_unfinished_action(
            [action.text for action in context.locked_case.actions],
            turns,
        )
        if next_action is None:
            action = (
                "当前方案中暂时没有可自动推进的新动作。请补充对方的新回复、"
                "平台处理结果、新材料或事实更正后，我再继续核对。"
            )
        else:
            action = next_action[1]
        return PipelineResult(
            session_id=run.session.id,
            turn_id=turn_id,
            audit_id=audit_id,
            followup_round=run.session.followup_round,
            can_ask_more=False,
            status=_session_pipeline_status(run.session),
            turn_kind="followup_answer",
            questions=(),
            limitations=(),
            provider_name=provider.name,
            provider_model=provider.model,
            provider_request_id=provider_request_id,
            usage=usage or UsageInfo(),
            scenario_id=run.playbook.id,
            playbook_version=run.playbook.version,
            coverage=coverage,
            reply=PipelineReply(
                text=(
                    "目前没有把“继续”当成你已经完成前一步；"
                    "先按当前尚未完成的动作处理并保留记录。"
                    if next_action is not None
                    else action
                ),
                suggested_actions=(action,) if next_action is not None else (),
            ),
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

    async def _ground_initial_plan(
        self,
        *,
        run: _RunState,
        artifacts: _PlanArtifacts,
        coverage: CoverageResult,
        playbook: Playbook,
        turns: Sequence[TurnRecord],
        message: str,
        extraction: ExtractionResult,
        provider: LLMProvider,
        usage_controls: ProviderUsageControls | None,
        client_identifier: str,
    ) -> tuple[_PlanArtifacts, UsageInfo, str | None]:
        packet = self._build_grounding_packet(
            message=message,
            coverage=coverage,
            facts=artifacts.evaluation.facts,
            turns=turns,
            actions=playbook.actions,
            evidence=playbook.evidence_now,
            limitations=artifacts.draft.plan.limitations,
            statutes=tuple(
                statute
                for statute in artifacts.statutes
                if statute.ref
                in {
                    citation.ref
                    for citation in artifacts.draft.plan.citations
                }
            ),
            basis_scope="case_specific",
            direct_answer=extraction.bounded_answer,
            formal_findings=(
                artifacts.evaluation.verdict_label,
                artifacts.evaluation.key_point,
            ),
            turn_intent=extraction.turn_intent,
            letter=artifacts.draft.plan.communication_guide,
        )
        grounded = build_local_answer(packet)
        grounded, usage, request_id = await self._compose_grounded_answer(
            run=run,
            packet=packet,
            draft=grounded,
            is_followup=bool(turns),
            provider=provider,
            usage_controls=usage_controls,
            client_identifier=client_identifier,
            base_usage=extraction.usage,
            base_request_id=extraction.request_id,
        )

        # communication_text 必须与 guide.message 逐字一致：前端
        # api.js 把两者不等视为契约破坏并拒绝整个响应，因此这里
        # 先算出唯一的正文，再同时写入两个字段。
        letter = self._grounded_letter(
            artifacts.draft.plan.communication_guide,
            grounded,
        )
        plan = artifacts.draft.plan.model_copy(
            update={
                "communication_guide": letter,
                "communication_text": letter.message,
                "summary": self._grounded_text(grounded)[:1000],
                "evidence_now": [
                    EvidenceItem(order=index, text=text)
                    for index, text in enumerate(
                        grounded.evidence,
                        start=1,
                    )
                ],
                "actions": [
                    ActionStep(order=index, text=text)
                    for index, text in enumerate(
                        grounded.actions,
                        start=1,
                    )
                ],
                "limitations": (
                    grounded.limitations
                    or artifacts.draft.plan.limitations
                ),
            }
        )
        locked_draft = artifacts.draft.model_copy(update={"plan": plan})
        run.begin("rendering")
        try:
            rendered = self.renderer.render(
                locked_draft,
                plan_template=playbook.templates.plan,
                evidence_template=playbook.templates.evidence,
            )
        except (TypeError, ValueError, OSError) as exc:
            raise DataIntegrityError(
                "template_render_failed",
                "固定模板渲染失败",
            ) from exc
        run.finish(
            citations=tuple(
                citation.ref for citation in locked_draft.plan.citations
            )
        )
        return (
            replace(
                artifacts,
                draft=locked_draft,
                rendered=rendered,
            ),
            usage,
            request_id,
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

        allowed_citations = {
            citation.ref for citation in context.locked_case.citations
        }
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

    def _resolve_provider(self, provider_id: str | None) -> LLMProvider:
        if self.settings.deployment_mode == "production":
            if provider_id is not None:
                raise InvalidProviderError()
            if self.provider_resolver is not None:
                return self.provider_resolver.resolve()
            if self.provider.name != "deepseek":
                raise InvalidProviderError()
            return self.provider
        if self.provider_resolver is not None:
            return self.provider_resolver.resolve(provider_id)
        if provider_id is None:
            return self.provider
        if provider_id.strip() != self.provider.name:
            raise InvalidProviderError()
        return self.provider

    def _usage_controls_for(
        self,
        provider: LLMProvider,
    ) -> ProviderUsageControls | None:
        return self.usage_controls_by_provider.get(provider.name)

    async def _call_extraction(
        self,
        *,
        run: _RunState,
        message: str,
        context: dict[str, object],
        evidence: tuple[AttachmentEvidenceContext, ...],
        client_identifier: str,
        provider: LLMProvider,
        usage_controls: ProviderUsageControls | None,
    ) -> ExtractionResult:
        if usage_controls is not None:
            usage_controls.before_call(client_identifier)
        if run.quota_call is not None:
            run.quota_call.reserve()
        extraction = await provider.extract_facts(
            message,
            context,
            evidence,
            timeout_seconds=self._remaining_provider_timeout(run),
        )
        self._verify_provider_identity(extraction, provider)
        if extraction.turn_intent in {"question", "stated_goal"} and (
            extraction.bounded_answer is None
        ):
            raise ProviderOutputError("模型未直接回答当前问题或诉求")
        if (
            requires_direct_answer(message)
            and classify_turn_intent(message) == "question"
            and (
            extraction.turn_intent != "question"
            or extraction.explicit_question is None
            )
        ):
            raise ProviderOutputError("模型未直接回答当前问题")
        if usage_controls is not None:
            extraction = extraction.model_copy(
                update={
                    "usage": usage_controls.after_call(
                        client_identifier,
                        extraction.usage,
                    )
                }
            )
        return extraction

    async def _call_continuation(
        self,
        *,
        run: _RunState,
        message: str,
        context: CaseContinuationContext,
        evidence: tuple[AttachmentEvidenceContext, ...],
        client_identifier: str,
        provider: LLMProvider,
        usage_controls: ProviderUsageControls | None,
    ) -> CaseContinuationResult:
        if usage_controls is not None:
            usage_controls.before_call(client_identifier)
        if run.quota_call is not None:
            run.quota_call.reserve()
        continuation = await provider.continue_case(
            message,
            context,
            evidence,
            timeout_seconds=self._remaining_provider_timeout(run),
        )
        self._verify_provider_identity(continuation, provider)
        if usage_controls is not None:
            continuation = continuation.model_copy(
                update={
                    "usage": usage_controls.after_call(
                        client_identifier,
                        continuation.usage,
                    )
                }
            )
        return continuation

    async def _compose_grounded_answer(
        self,
        *,
        run: _RunState,
        packet: GroundingPacket,
        draft: GroundedAnswerDraft,
        is_followup: bool,
        provider: LLMProvider,
        usage_controls: ProviderUsageControls | None,
        client_identifier: str,
        base_usage: UsageInfo,
        base_request_id: str | None,
    ) -> tuple[GroundedAnswerDraft, UsageInfo, str | None]:
        if not should_compose_grounded_answer(
            packet,
            draft,
            is_followup=is_followup,
        ):
            return draft, base_usage, base_request_id

        run.begin("composition")
        try:
            remaining = self._remaining_provider_timeout(run)
            if remaining < 0.25:
                raise ProviderError(
                    "provider_timeout",
                    "模型成文阶段剩余时间不足",
                    retryable=True,
                )
            if usage_controls is not None:
                usage_controls.before_call(client_identifier)
            composition = await provider.compose_grounded_answer(
                packet,
                timeout_seconds=remaining,
            )
            if (
                composition.provider != provider.name
                or composition.model != provider.model
            ):
                raise ProviderOutputError("成文模型身份元数据无效")
            try:
                merged = merge_grounded_answer(packet, draft, composition)
            except ValueError as exc:
                raise ProviderOutputError("成文结果超出依据包边界") from exc
            composition_usage = composition.usage
            if usage_controls is not None:
                composition_usage = usage_controls.after_call(
                    client_identifier,
                    composition_usage,
                )
            usage = _merge_usage_info(base_usage, composition_usage)
            run.finish(
                citations=tuple(merged.used_statute_ids),
            )
            return (
                merged,
                usage,
                composition.request_id or base_request_id,
            )
        except SafeApplicationError as exc:
            run.provider_degraded = True
            run.finish(
                status="degraded",
                error_category=exc.code,
            )
            return draft, base_usage, base_request_id

    @staticmethod
    def _remaining_provider_timeout(run: _RunState) -> float:
        if run.provider_deadline is None:
            raise ProviderError("provider_timeout", retryable=True)
        remaining = run.provider_deadline - monotonic()
        if remaining <= 0:
            raise ProviderError("provider_timeout", retryable=True)
        return remaining

    def _formal_coverage(
        self,
        playbook: Playbook,
        *,
        confidence: float | None = None,
    ) -> CoverageResult:
        try:
            topic = self.topics.get(playbook.id)
        except LookupError as exc:
            raise DataIntegrityError(
                "formal_topic_missing",
                "正式场景缺少 Topic 注册项",
            ) from exc
        return CoverageResult(
            mode="formal",
            topic_id=topic.id,
            topic_label=topic.label,
            confidence=confidence,
            playbook_id=playbook.id,
            notice="已进入本地核验的正式处理流程。",
            risk_flags=[],
        )

    @staticmethod
    def _validate_current_playbook_candidate(
        extraction: ExtractionResult,
        playbook: Playbook,
    ) -> None:
        if extraction.candidate_topic_id not in {
            playbook.id,
            "unknown",
            "unsupported",
        }:
            raise ProviderOutputError("模型返回了会话之外的场景")

    @staticmethod
    def _guidance_facts(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            name: value
            for name, value in existing.items()
            if name in GENERIC_FACT_NAMES
        }

    @staticmethod
    def _conversation_context(
        *,
        turns: Sequence[TurnRecord],
        previous_topic: Mapping[str, Any] | None,
        message: str,
        existing_facts: Mapping[str, Any],
    ) -> dict[str, object]:
        recent_conversation = []
        for turn in turns[-_RECENT_CONVERSATION_TURNS:]:
            visible = project_response(turn.response)
            assistant_units = visible.core_units[:8]
            assistant_reply = "；".join(assistant_units)
            recent_conversation.append(
                {
                    "user_message": turn.user_message[
                        :_RECENT_USER_MESSAGE_CHARS
                    ],
                    "assistant_reply": assistant_reply[
                        :_RECENT_ASSISTANT_REPLY_CHARS
                    ],
                }
            )
        previous_topic_id = (
            previous_topic.get("topic_id")
            if previous_topic is not None
            else None
        )
        previous_topic_label = (
            previous_topic.get("topic_label")
            if previous_topic is not None
            else None
        )
        return {
            "previous_topic_id": (
                previous_topic_id
                if isinstance(previous_topic_id, str)
                else None
            ),
            "previous_topic_label": (
                previous_topic_label
                if isinstance(previous_topic_label, str)
                else None
            ),
            "confirmed_facts": dict(existing_facts),
            "is_followup": bool(turns),
            "is_direct_question": is_direct_question(message),
            "recent_conversation": recent_conversation,
        }

    def _build_grounding_packet(
        self,
        *,
        message: str,
        coverage: CoverageResult,
        facts: Mapping[str, Any],
        turns: Sequence[TurnRecord],
        actions: Sequence[str],
        evidence: Sequence[str],
        limitations: Sequence[str],
        statutes: Sequence[StatuteRecord] = (),
        basis_scope: Literal["case_specific", "general"] = "general",
        direct_answer: str | None = None,
        next_question: str | None = None,
        formal_findings: Sequence[str] = (),
        turn_intent: str | None = None,
        letter: CommunicationGuide | None = None,
    ) -> GroundingPacket:
        intent = turn_intent or classify_turn_intent(message)
        recent_messages = [
            turn.user_message.strip()
            for turn in turns[-4:]
            if turn.user_message.strip()
        ]
        if not recent_messages or recent_messages[-1] != message:
            recent_messages.append(message)
        case_summary = "；".join(recent_messages)[-2400:]

        current_goal: str | None = None
        if intent == "stated_goal":
            current_goal = message
        else:
            for turn in reversed(turns):
                if classify_turn_intent(turn.user_message) == "stated_goal":
                    current_goal = turn.user_message[:500]
                    break

        completed_actions = [
            turn.user_message[:500]
            for turn in turns[-8:]
            if classify_turn_intent(turn.user_message) == "completed_action"
        ]
        if intent == "completed_action":
            completed_actions.append(message[:500])
        completed_actions = list(dict.fromkeys(completed_actions))[-8:]

        approved_actions, evidence_targets = self._grounding_content(
            coverage.topic_id,
            case_summary,
            actions,
            evidence,
        )
        previous_units: list[str] = list(questions_asked(turns))
        for turn in turns[-4:]:
            previous_units.extend(
                project_response(turn.response).core_units[:4]
            )
        previous_units = list(dict.fromkeys(previous_units))[-12:]

        grounding_statutes = [
            GroundingStatute.from_statute(
                statute,
                basis_scope=basis_scope,
            )
            for statute in statutes
        ]
        # 正文三字段来自已构建好的模板 guide：收件人和沟通目标作为改写红线，
        # message 作为草稿。模型只能在草稿基础上重写，不能凭空生成正文，
        # 因此 letter 缺失时三者一并为 None，_merged_letter 会拒绝任何正文。
        letter_recipient = letter.recipient if letter is not None else None
        letter_objective = letter.objective if letter is not None else None
        letter_draft = letter.message if letter is not None else None
        return GroundingPacket(
            current_message=message,
            turn_intent=intent,  # type: ignore[arg-type]
            case_summary=case_summary or message,
            confirmed_facts=dict(facts),
            current_goal=current_goal,
            completed_actions=completed_actions,
            coverage_mode=coverage.mode,
            topic_id=coverage.topic_id,
            topic_label=coverage.topic_label,
            formal_findings=list(dict.fromkeys(formal_findings))[:8],
            allowed_actions=approved_actions,
            evidence_targets=evidence_targets,
            verified_statutes=grounding_statutes,
            limitations=list(dict.fromkeys(limitations))[:12],
            previously_answered=previous_units,
            one_allowed_next_question=next_question,
            direct_answer_draft=direct_answer,
            letter_recipient=letter_recipient,
            letter_objective=letter_objective,
            letter_draft=letter_draft,
        )

    @staticmethod
    def _grounding_content(
        topic_id: str,
        case_summary: str,
        actions: Sequence[str],
        evidence: Sequence[str],
    ) -> tuple[list[str], list[str]]:
        if any(
            marker in case_summary
            for marker in ("发错货", "错发", "发错", "补发", "重新发货")
        ):
            return (
                [
                    (
                        "通过平台聊天向商家发送补发要求，写明订单号、错发商品、"
                        "应补发商品和答复期限，并保存发送及已读记录。"
                    ),
                    (
                        "商家逾期不回复时，通过订单售后或平台客服提交投诉，"
                        "附上订单、错发照片、面单和此前催告记录。"
                    ),
                    (
                        "平台仍不处理时，保存工单和处理结果，再向消费投诉渠道反映。"
                    ),
                ],
                [
                    "保存订单详情、实际收到商品、快递面单和外包装的照片。",
                    "保存向商家提出补发要求、答复期限、已读状态和全部回复记录。",
                    "保留平台售后申请、工单编号、上传材料和平台处理结果。",
                ],
            )
        normalized_actions = list(
            dict.fromkeys(item.strip() for item in actions if item.strip())
        )[:8]
        normalized_evidence = list(
            dict.fromkeys(item.strip() for item in evidence if item.strip())
        )[:8]
        if not normalized_actions:
            normalized_actions = [
                "通过可留痕渠道向对方说明事实、当前诉求和希望回复的时间。"
            ]
        if not normalized_evidence:
            normalized_evidence = [
                "保存原始记录、时间线、对方身份信息和每次沟通的完整内容。"
            ]
        return normalized_actions, normalized_evidence

    @staticmethod
    def _grounded_text(draft: GroundedAnswerDraft) -> str:
        parts = [draft.direct_reply, *draft.legal_explanation]
        return "\n\n".join(part for part in parts if part).strip()[:1200]

    @staticmethod
    def _grounded_letter(
        template: CommunicationGuide,
        draft: GroundedAnswerDraft,
    ) -> CommunicationGuide:
        """用模型改写后的正文替换模板正文，其余字段一律保持模板值。

        draft.letter_body 已经过 merge_grounded_answer 的正文校验（禁止代
        对方许诺义务、禁止替用户放弃权利、收件人不得被改写），到这里只需
        决定是否替换。模型未给正文或校验后为空时保留模板，保证正文永远
        可用，不会因为模型缺字段而变空。
        """
        letter = draft.letter_body
        if letter is None or not letter.strip():
            return template
        return template.model_copy(update={"message": letter.strip()})

    @staticmethod
    def _grounded_followup_text(draft: GroundedAnswerDraft) -> str:
        parts = [draft.direct_reply, *draft.legal_explanation]
        if draft.evidence:
            evidence = [
                item.strip().rstrip("。；;")
                for item in draft.evidence
                if item.strip().rstrip("。；;")
            ]
            if evidence:
                parts.append("重点证据：" + "；".join(evidence) + "。")
        if draft.next_question:
            parts.append(draft.next_question)
        return "\n\n".join(part for part in parts if part).strip()[:1200]

    @staticmethod
    def _grounded_citations(
        packet: GroundingPacket,
        draft: GroundedAnswerDraft,
    ) -> tuple[LegalCitation, ...]:
        by_id = {
            statute.statute_id: statute
            for statute in packet.verified_statutes
        }
        citations: list[LegalCitation] = []
        for statute_id in draft.used_statute_ids:
            statute = by_id.get(statute_id)
            if statute is None or statute.effective_date is None:
                raise ProviderOutputError("成文引用无法映射到已核验法条")
            citations.append(
                LegalCitation(
                    ref=statute.statute_id,
                    law_name=statute.law_name,
                    article_no=statute.article_no,
                    content=statute.verified_text,
                    effective_date=statute.effective_date,
                    source_url=statute.official_url,
                    basis_scope=statute.basis_scope,
                    applicability_notice=statute.applicability_notice,
                )
            )
        return tuple(citations)

    async def _unverified_progress_result(
        self,
        *,
        run: _RunState,
        session: SessionRecord,
        turn_id: str,
        audit_id: str,
        coverage: CoverageResult,
        provider: LLMProvider,
        turns: Sequence[TurnRecord],
        message: str,
        client_identifier: str,
        usage_controls: ProviderUsageControls | None,
        extraction: ExtractionResult,
    ) -> PipelineResult:
        current_stage = derive_unverified_stage(
            turns,
            topic_id=coverage.topic_id,
        )
        if current_stage >= 7:
            raise CaseNoProgressError()
        stage = requested_unverified_stage(
            message,
            current_stage,
            turn_intent=extraction.turn_intent,
        )
        base_guidance = self.guidance_builder.build(
            coverage,
            facts=self._guidance_facts(session.facts),
            message=message,
        )
        if extraction.turn_intent == "completed_action":
            progress = self.guidance_builder.build_unverified_stage(
                coverage,
                stage=stage,
                message=message,
            )
            actions = [progress.action]
            next_question = progress.next_question
            direct_answer = progress.text
        else:
            actions = base_guidance.actions
            next_question = base_guidance.next_question
            direct_answer = extraction.bounded_answer

        run.begin("retrieval")
        try:
            statutes = self._retrieve_general_statutes(
                topic_id=coverage.topic_id,
                message=message,
                turns=turns,
            )
            run.finish(citations=tuple(item.ref for item in statutes))
        except DataIntegrityError as exc:
            statutes = []
            run.finish(status="degraded", error_category=exc.code)
        packet = self._build_grounding_packet(
            message=message,
            coverage=coverage,
            facts=session.facts,
            turns=turns,
            actions=actions,
            evidence=base_guidance.evidence_now,
            limitations=base_guidance.limitations,
            statutes=statutes,
            basis_scope="general",
            direct_answer=direct_answer,
            next_question=next_question,
            turn_intent=extraction.turn_intent,
        )
        draft = build_local_answer(packet)
        draft, usage, request_id = await self._compose_grounded_answer(
            run=run,
            packet=packet,
            draft=draft,
            is_followup=True,
            provider=provider,
            usage_controls=usage_controls,
            client_identifier=client_identifier,
            base_usage=extraction.usage,
            base_request_id=extraction.request_id,
        )
        citations = self._grounded_citations(packet, draft)
        reply_text = self._grounded_followup_text(draft)
        run.begin("followup")
        return PipelineResult(
            session_id=session.id,
            turn_id=turn_id,
            audit_id=audit_id,
            followup_round=session.followup_round,
            can_ask_more=draft.next_question is not None,
            status="need_more_facts",
            turn_kind="followup_answer",
            questions=(),
            limitations=(),
            provider_name=provider.name,
            provider_model=provider.model,
            provider_request_id=request_id,
            usage=usage,
            scenario_id=None,
            coverage=None,
            guidance=None,
            reply=PipelineReply(
                text=reply_text,
                suggested_actions=tuple(draft.actions[:3]),
                citation_refs=tuple(item.ref for item in citations),
            ),
            reply_citations=citations,
        )

    def _emergency_result(
        self,
        *,
        session: SessionRecord,
        turn_id: str,
        audit_id: str,
        coverage: CoverageResult,
        facts: dict[str, Any],
        provider: LLMProvider,
        turns: Sequence[TurnRecord],
        message: str = "",
        extraction: ExtractionResult | None = None,
    ) -> PipelineResult:
        previous_risk = next(
            (
                response_coverage
                for turn in reversed(turns)
                if isinstance(
                    (
                        response_coverage := turn.response.get(
                            "coverage"
                        )
                    ),
                    Mapping,
                )
                and response_coverage.get("mode")
                == "emergency_guidance"
                and set(response_coverage.get("risk_flags", ())).intersection(
                    coverage.risk_flags
                )
            ),
            None,
        )
        if previous_risk is None:
            return self._guidance_result(
                session=session,
                turn_id=turn_id,
                audit_id=audit_id,
                coverage=coverage,
                facts=facts,
                provider=provider,
                message=message,
                extraction=extraction,
            )

        primary_flag = coverage.risk_flags[0]
        question = next_emergency_question(primary_flag, turns)
        if question is None:
            raise CaseNoProgressError(
                "当前紧急指导已经给出；请告知是否已到安全地点、"
                "获得救助、完成止损或风险已经解除"
            )
        return PipelineResult(
            session_id=session.id,
            turn_id=turn_id,
            audit_id=audit_id,
            followup_round=session.followup_round,
            can_ask_more=True,
            status="escalate",
            turn_kind="followup_answer",
            questions=(),
            limitations=(),
            provider_name=(
                extraction.provider
                if extraction is not None
                else provider.name
            ),
            provider_model=(
                extraction.model
                if extraction is not None
                else provider.model
            ),
            provider_request_id=(
                extraction.request_id
                if extraction is not None
                else None
            ),
            usage=(
                extraction.usage
                if extraction is not None
                else UsageInfo()
            ),
            scenario_id=None,
            coverage=None,
            guidance=None,
            reply=PipelineReply(
                text=question,
                suggested_actions=(
                    self.guidance_builder.emergency_anchor(coverage),
                ),
            ),
        )

    @staticmethod
    def _has_emergency_interlude(
        turns: Sequence[TurnRecord],
    ) -> bool:
        return any(
            isinstance((coverage := turn.response.get("coverage")), Mapping)
            and coverage.get("mode") == "emergency_guidance"
            for turn in turns
        )

    @staticmethod
    def _is_unverified_status_update(message: str) -> bool:
        return has_unverified_stage_signal(message)

    async def _resume_after_emergency(
        self,
        *,
        run: _RunState,
        turns: list[TurnRecord],
        message: str,
        active_jurisdiction: str | None,
        provider: LLMProvider,
        client_identifier: str,
        usage_controls: ProviderUsageControls | None,
        turn_id: str,
        audit_id: str,
        evidence: tuple[AttachmentEvidenceContext, ...],
        extraction_context: dict[str, object],
    ) -> PipelineResult | None:
        if run.session is None:
            raise RuntimeError("恢复紧急插曲前缺少会话")
        if (
            run.playbook is not None
            and has_historical_plan(turns)
        ):
            run.begin("retrieval")
            statutes = self._retrieve_statutes(run.playbook)
            run.finish(
                citations=tuple(statute.ref for statute in statutes)
            )
            context = build_case_continuation_context(
                playbook=run.playbook,
                registry=self.registry,
                existing_facts=run.session.facts,
                statutes=statutes,
                turns=turns,
            )
            run.begin("case_continuation")
            continuation = await self._call_continuation(
                run=run,
                message=message,
                context=context,
                evidence=evidence,
                client_identifier=client_identifier,
                provider=provider,
                usage_controls=usage_controls,
            )
            run.finish()
            run.begin("followup")
            result = self._next_formal_action_result(
                run=run,
                turns=turns,
                context=context,
                coverage=self._formal_coverage(run.playbook),
                provider=provider,
                turn_id=turn_id,
                audit_id=audit_id,
                usage=continuation.usage,
                provider_request_id=continuation.request_id,
            )
            run.finish()
            return self._persist_success(
                run,
                result,
                message=message,
                facts=run.session.facts,
                rule_matches=[],
                jurisdiction=active_jurisdiction,
            )

        historical_unverified = latest_coverage(
            turns,
            mode="unverified_guidance",
        )
        if historical_unverified is None:
            return None
        coverage = CoverageResult.model_validate(
            historical_unverified
        )
        run.begin("extraction")
        extraction = await self._call_extraction(
            run=run,
            message=message,
            context=extraction_context,
            evidence=evidence,
            client_identifier=client_identifier,
            provider=provider,
            usage_controls=usage_controls,
        )
        run.finish()
        run.begin("followup")
        result = await self._unverified_progress_result(
            run=run,
            session=run.session,
            turn_id=turn_id,
            audit_id=audit_id,
            coverage=coverage,
            provider=provider,
            turns=turns,
            message=message,
            client_identifier=client_identifier,
            usage_controls=usage_controls,
            extraction=extraction,
        )
        run.finish()
        return self._persist_success(
            run,
            result,
            message=message,
            facts=run.session.facts,
            rule_matches=[],
            jurisdiction=active_jurisdiction,
            preserve_session_state=True,
        )

    def _guidance_result(
        self,
        *,
        session: SessionRecord,
        turn_id: str,
        audit_id: str,
        coverage: CoverageResult,
        facts: dict[str, Any],
        provider: LLMProvider,
        message: str = "",
        extraction: ExtractionResult | None = None,
    ) -> PipelineResult:
        guidance = self.guidance_builder.build(
            coverage,
            facts=facts,
            message=message,
        )
        if (
            extraction is not None
            and extraction.explicit_question is not None
            and extraction.bounded_answer is not None
        ):
            guidance = guidance.model_copy(
                update={"direct_answer": extraction.bounded_answer}
            )
        emergency = coverage.mode == "emergency_guidance"
        return PipelineResult(
            session_id=session.id,
            turn_id=turn_id,
            audit_id=audit_id,
            followup_round=session.followup_round,
            can_ask_more=guidance.next_question is not None,
            status="escalate" if emergency else "need_more_facts",
            turn_kind=(
                "emergency_guidance"
                if emergency
                else "unverified_guidance"
            ),
            questions=(),
            limitations=tuple(guidance.limitations),
            provider_name=(
                extraction.provider
                if extraction is not None
                else provider.name
            ),
            provider_model=(
                extraction.model
                if extraction is not None
                else provider.model
            ),
            provider_request_id=(
                extraction.request_id
                if extraction is not None
                else None
            ),
            usage=(
                extraction.usage
                if extraction is not None
                else UsageInfo()
            ),
            scenario_id=None,
            coverage=coverage,
            guidance=guidance,
        )

    async def _grounded_guidance_result(
        self,
        *,
        run: _RunState,
        session: SessionRecord,
        turn_id: str,
        audit_id: str,
        coverage: CoverageResult,
        facts: dict[str, Any],
        provider: LLMProvider,
        turns: Sequence[TurnRecord],
        message: str,
        client_identifier: str,
        usage_controls: ProviderUsageControls | None,
        extraction: ExtractionResult,
    ) -> PipelineResult:
        guidance = self.guidance_builder.build(
            coverage,
            facts=facts,
            message=message,
        )
        run.begin("retrieval")
        try:
            statutes = self._retrieve_general_statutes(
                topic_id=coverage.topic_id,
                message=message,
                turns=turns,
            )
            run.finish(citations=tuple(item.ref for item in statutes))
        except DataIntegrityError as exc:
            statutes = []
            run.finish(status="degraded", error_category=exc.code)

        packet = self._build_grounding_packet(
            message=message,
            coverage=coverage,
            facts=facts,
            turns=turns,
            actions=guidance.actions,
            evidence=guidance.evidence_now,
            limitations=guidance.limitations,
            statutes=statutes,
            basis_scope="general",
            direct_answer=extraction.bounded_answer,
            next_question=guidance.next_question,
            turn_intent=extraction.turn_intent,
            letter=guidance.communication_guide,
        )
        draft = build_local_answer(packet)
        draft, usage, request_id = await self._compose_grounded_answer(
            run=run,
            packet=packet,
            draft=draft,
            is_followup=bool(turns),
            provider=provider,
            usage_controls=usage_controls,
            client_identifier=client_identifier,
            base_usage=extraction.usage,
            base_request_id=extraction.request_id,
        )
        citations = self._grounded_citations(packet, draft)
        guidance = guidance.model_copy(
            update={
                "direct_answer": self._grounded_text(draft),
                "actions": draft.actions,
                "evidence_now": draft.evidence,
                "limitations": draft.limitations,
                "next_question": draft.next_question,
                "communication_guide": self._grounded_letter(
                    guidance.communication_guide,
                    draft,
                ),
            }
        )
        run.begin("followup")
        return PipelineResult(
            session_id=session.id,
            turn_id=turn_id,
            audit_id=audit_id,
            followup_round=session.followup_round,
            can_ask_more=guidance.next_question is not None,
            status="need_more_facts",
            turn_kind="unverified_guidance",
            questions=(),
            limitations=tuple(guidance.limitations),
            provider_name=provider.name,
            provider_model=provider.model,
            provider_request_id=request_id,
            usage=usage,
            scenario_id=None,
            coverage=coverage,
            guidance=guidance,
            reply_citations=citations,
        )

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
        provider: LLMProvider,
    ) -> None:
        if (
            result.provider != provider.name
            or result.model != provider.model
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
            compatible_existing = {
                name: value
                for name, value in existing.items()
                if name in playbook.slot_names
            }
            merged = {**compatible_existing, **new_facts}
            validated = self.registry.validate_facts(
                playbook.id,
                merged,
                apply_defaults=False,
            )
        except ValueError as exc:
            raise ProviderOutputError("模型返回的事实槽位无效") from exc
        return validated.facts, validated.missing_required

    async def _followup_result(
        self,
        *,
        run: _RunState,
        session: SessionRecord,
        turn_id: str,
        audit_id: str,
        extraction: ExtractionResult,
        scenario_id: str | None,
        facts: dict[str, Any],
        question_specs: Sequence[tuple[str, str]],
        turns: Sequence[TurnRecord],
        playbook_version: str | None = None,
        coverage: CoverageResult | None = None,
        message: str,
        playbook: Playbook,
        provider: LLMProvider,
        usage_controls: ProviderUsageControls | None,
        client_identifier: str,
    ) -> PipelineResult:
        if session.followup_round < 2:
            followup_round = session.followup_round + 1
            asked = set(questions_asked(turns))
            selected_question = next(
                (
                    question
                    for question, _slot_type in question_specs
                    if normalize_visible_text(question) not in asked
                ),
                None,
            )
            if selected_question is None:
                selected_question = next(
                    (
                        precise
                        for question, slot_type in question_specs
                        if (
                            precise := more_precise_question(
                                question,
                                slot_type=slot_type,
                            )
                        )
                        and normalize_visible_text(precise) not in asked
                    ),
                    None,
                )
            if selected_question is None:
                raise CaseNoProgressError()
            selected_questions = (selected_question,)
            can_ask_more = followup_round < 2
            limitations: tuple[str, ...] = ()
        else:
            followup_round = 2
            selected_questions = ()
            can_ask_more = False
            limitation = "已达到两轮追问上限，现有事实不足以形成确定性判断。"
            if limitation_already_served(turns, limitation):
                raise CaseNoProgressError()
            limitations = (limitation,)
        effective_coverage = coverage or self._formal_coverage(playbook)
        packet = self._build_grounding_packet(
            message=message,
            coverage=effective_coverage,
            facts=facts,
            turns=turns,
            actions=playbook.actions,
            evidence=playbook.evidence_now,
            limitations=playbook.limitations,
            direct_answer=extraction.bounded_answer,
            next_question=(
                selected_questions[0] if selected_questions else None
            ),
            turn_intent=extraction.turn_intent,
        )
        draft = build_local_answer(packet)
        draft, usage, request_id = await self._compose_grounded_answer(
            run=run,
            packet=packet,
            draft=draft,
            is_followup=bool(turns),
            provider=provider,
            usage_controls=usage_controls,
            client_identifier=client_identifier,
            base_usage=extraction.usage,
            base_request_id=extraction.request_id,
        )
        reply_draft = draft.model_copy(update={"next_question": None})
        run.begin("followup")
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
            provider_request_id=request_id,
            usage=usage,
            scenario_id=scenario_id,
            playbook_version=playbook_version,
            coverage=effective_coverage,
            reply=PipelineReply(
                text=self._grounded_followup_text(reply_draft),
                suggested_actions=tuple(draft.actions[:3]),
            ),
        )

    def _retrieve_statutes(
        self,
        playbook: Playbook,
    ) -> list[StatuteRecord]:
        return self._retrieve_statutes_by_refs(
            [basis.ref for basis in playbook.legal_basis]
        )

    def _retrieve_general_statutes(
        self,
        *,
        topic_id: str,
        message: str,
        turns: Sequence[TurnRecord] = (),
    ) -> list[StatuteRecord]:
        recent_messages = [
            turn.user_message.strip()
            for turn in turns[-4:]
            if turn.user_message.strip()
        ]
        if not recent_messages or recent_messages[-1] != message:
            recent_messages.append(message)
        basis_context = "；".join(recent_messages)[-4000:]
        refs = general_basis_refs(topic_id, basis_context)
        if refs:
            return self._retrieve_statutes_by_refs(refs)
        # 精选映射没有覆盖这个主题（topic_id 落到 unknown 时必然如此）。
        # 原先这里直接返回空，用户拿不到任何法条——甲醛这类问题就是这样
        # 一条依据都没有。改为按口语触发词推断主题，再取该主题下人工
        # 确认过的条文。
        #
        # 这里特意不让 BM25 直接挑条文：实测检索分数无法区分条文立场，
        # 甲醛案中对用户不利的第七百一十一条拿到 50.19 分，而正确的
        # 第七百一十三条只有 25.46，任何阈值都会连正确条文一起挡掉。
        # 推断主题只回答「这是哪类纠纷」，条文仍由精选映射决定。
        inferred = infer_topic(basis_context)
        if inferred is None or inferred == topic_id:
            return []
        fallback_refs = general_basis_refs(inferred, basis_context)
        if not fallback_refs:
            return []
        return self._retrieve_statutes_by_refs(fallback_refs)

    def _retrieve_statutes_by_refs(
        self,
        refs: Sequence[str],
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
                for ref in refs:
                    statute = get_statute_by_ref(connection, ref)
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
        preserve_session_state: bool = False,
        consume_quota: bool = True,
        allow_duplicate: bool = False,
    ) -> PipelineResult:
        if run.session is None:
            raise RuntimeError("持久化前缺少会话")
        if run.provider_degraded:
            consume_quota = False
        candidate = project_response(result.public_payload())
        if not allow_duplicate:
            duplicate = find_duplicate(
                candidate,
                project_turns(run.turns),
            )
            if duplicate.duplicate:
                raise CaseNoProgressError()

        attachment_binding = None
        if bind_attachments and run.attachment_ids:
            if run.reservation_id is None:
                raise RuntimeError("附件绑定前缺少有效预留")
            attachment_binding = AttachmentBindingCommand(
                reservation_id=run.reservation_id,
                attachment_ids=run.attachment_ids,
            )

        run.begin("persistence")
        conversation_store: ConversationRepository | None = (
            self.store if run.persist else run.transient_store
        )
        if conversation_store is None:
            run.finish()
            if run.quota_call is not None:
                if consume_quota:
                    run.quota_call.succeed()
                else:
                    run.quota_call.refund()
            run.begin("response")
            run.finish()
            return result

        stored_result = replace(result, attachments=())
        session_scenario_id = (
            run.session.scenario_id
            if preserve_session_state
            else result.scenario_id
        )
        session_facts = (
            run.session.facts
            if preserve_session_state
            else facts
        )
        session_followup_round = (
            run.session.followup_round
            if preserve_session_state
            else result.followup_round
        )
        session_status = (
            run.session.status
            if (
                preserve_session_state
                and run.session.status
                in {"need_more_facts", "ready", "escalate"}
            )
            else result.status
        )
        session_jurisdiction = (
            run.session.jurisdiction
            if preserve_session_state
            else jurisdiction
        )
        conversation_store.persist_session_turn(
            ConsultationCommitCommand(
                owner_id=run.session.owner_id,
                session_id=run.session.id,
                session=SessionUpdateCommand(
                    scenario_id=session_scenario_id,
                    facts=session_facts,
                    followup_round=session_followup_round,
                    status=session_status,
                    jurisdiction=session_jurisdiction,
                ),
                turn=TurnWriteCommand(
                    turn_id=result.turn_id,
                    user_message=message,
                    facts=facts,
                    rule_matches=tuple(rule_matches),
                    response=stored_result.public_payload(),
                    provider_name=result.provider_name,
                    provider_model=result.provider_model,
                    provider_request_id=result.provider_request_id,
                    usage=result.usage,
                ),
                attachment_binding=attachment_binding,
                expected_latest_turn_id=(
                    run.turns[-1].id if run.turns else None
                ),
                comparison_units=(
                    ()
                    if allow_duplicate
                    else comparison_units(candidate)
                ),
            )
        )
        if run.quota_call is not None:
            if consume_quota:
                run.quota_call.succeed()
            else:
                run.quota_call.refund()
        run.finish()

        if not run.persist:
            run.begin("response")
            run.finish()
            return result

        public_attachments: tuple[AttachmentTurnPublic, ...] = ()
        if attachment_binding is not None:
            public_attachments = tuple(
                attachment_turn_public(record)
                for record in self.attachments.list_for_turn(
                    result.turn_id,
                    owner_id=run.session.owner_id,
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
                owner_id=run.session.owner_id,
            )
        return public_result

    def _persist_failure(
        self,
        run: _RunState,
        audit_id: str,
        error_category: str,
    ) -> None:
        if run.session is None or not run.persist:
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
                    owner_id=run.session.owner_id,
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


def _merge_usage_info(left: UsageInfo, right: UsageInfo) -> UsageInfo:
    estimated_cost: float | None = None
    if (
        left.estimated_cost_usd is not None
        or right.estimated_cost_usd is not None
    ):
        estimated_cost = (
            (left.estimated_cost_usd or 0.0)
            + (right.estimated_cost_usd or 0.0)
        )
    return UsageInfo(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        estimated_cost_usd=estimated_cost,
    )


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
