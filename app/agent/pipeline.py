from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from app.agent.errors import (
    DataIntegrityError,
    ProviderError,
    ProviderOutputError,
    RequestInputError,
    SafeApplicationError,
    StorageUnavailableError,
)
from app.agent.models import ExtractionResult, UsageInfo
from app.config import Settings
from app.db.models import SessionRecord
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
    LockedConsultationDraft,
    RenderedDocuments,
    build_consultation_draft,
)
from app.rendering.renderer import PlanRenderer
from app.retrieval.database import connect_database, get_metadata
from app.retrieval.search import search_connection


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
class PipelineResult:
    session_id: str
    turn_id: str
    audit_id: str
    followup_round: int
    can_ask_more: bool
    status: PipelineStatus
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
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "audit_id": self.audit_id,
            "followup_round": self.followup_round,
            "can_ask_more": self.can_ask_more,
            "status": self.status,
            "verdict": verdict,
            "plan": plan,
            "questions": list(self.questions),
            "limitations": list(self.limitations),
            "citations": citations,
            "usage": {
                "provider": self.provider_name,
                "model": self.provider_model,
                "request_id": self.provider_request_id,
                **self.usage.model_dump(mode="json"),
            },
        }


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
        registry: PlaybookRegistry,
        provider: LLMProvider,
        jurisdictions: JurisdictionRegistry,
        renderer: PlanRenderer,
        usage_controls: ProviderUsageControls | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
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
    ) -> PipelineResult:
        run = _RunState()
        audit_id = str(uuid4())
        turn_id = str(uuid4())
        extraction: ExtractionResult | None = None
        try:
            normalized_message = self._validate_message(message)
            normalized_jurisdiction = _optional_text(jurisdiction)
            run.finish()

            run.begin("session")
            if session_id is None:
                run.session = self.store.create_session(
                    jurisdiction=normalized_jurisdiction
                )
            else:
                run.session = self.store.require_session(session_id)
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

            run.begin("extraction")
            if self.usage_controls is not None:
                self.usage_controls.before_call(client_identifier)
            extraction = await self.provider.extract_facts(
                normalized_message,
                context,
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

            run.begin("retrieval")
            statutes = self._retrieve_statutes(
                run.playbook,
                normalized_message,
                facts,
            )
            citation_refs = tuple(statute.ref for statute in statutes)
            run.finish(citations=citation_refs)

            run.begin("rules")
            try:
                evaluation = evaluate_playbook(run.playbook, facts)
            except ValueError as exc:
                raise DataIntegrityError(
                    "rule_evaluation_failed",
                    "本地规则无法安全求值",
                ) from exc
            escalation_messages = matching_escalations(
                run.playbook,
                evaluation.facts,
            )
            run.finish()

            run.begin("jurisdiction")
            jurisdiction_outcome = evaluate_jurisdiction(
                run.playbook,
                self.jurisdictions,
                jurisdiction=active_jurisdiction,
                facts=evaluation.facts,
            )
            run.finish()

            run.begin("draft")
            try:
                draft = build_consultation_draft(
                    run.playbook,
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
                    plan_template=run.playbook.templates.plan,
                    evidence_template=run.playbook.templates.evidence,
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
            result = PipelineResult(
                session_id=run.session.id,
                turn_id=turn_id,
                audit_id=audit_id,
                followup_round=run.session.followup_round,
                can_ask_more=False,
                status=status,
                questions=(),
                limitations=(),
                provider_name=extraction.provider,
                provider_model=extraction.model,
                provider_request_id=extraction.request_id,
                usage=extraction.usage,
                scenario_id=run.playbook.id,
                playbook_version=run.playbook.version,
                draft=draft,
                rendered=rendered,
            )
            return self._persist_success(
                run,
                result,
                message=normalized_message,
                facts=evaluation.facts,
                rule_matches=[
                    match.model_dump(mode="json")
                    for match in evaluation.matches
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
        extraction: ExtractionResult,
    ) -> None:
        if (
            extraction.provider != self.provider.name
            or extraction.model != self.provider.model
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
        message: str,
        facts: dict[str, Any],
    ):
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
                forced_refs = [
                    basis.ref
                    for basis in playbook.legal_basis
                ]
                fact_text = " ".join(
                    str(value)
                    for value in facts.values()
                    if value is not None
                )
                hits = search_connection(
                    connection,
                    f"{message} {fact_text}",
                    limit=max(10, len(forced_refs)),
                    forced_refs=forced_refs,
                )
            finally:
                connection.close()
        except DataIntegrityError:
            raise
        except (FileNotFoundError, LookupError, sqlite3.Error) as exc:
            raise DataIntegrityError(
                "statute_retrieval_failed",
                "已核验法条检索失败",
            ) from exc
        return [hit.statute for hit in hits]

    def _persist_success(
        self,
        run: _RunState,
        result: PipelineResult,
        *,
        message: str,
        facts: dict[str, Any],
        rule_matches: list[dict[str, Any]],
        jurisdiction: str | None,
    ) -> PipelineResult:
        if run.session is None:
            raise RuntimeError("持久化前缺少会话")

        run.begin("persistence")
        self.store.update_session(
            run.session.id,
            scenario_id=result.scenario_id,
            facts=facts,
            followup_round=result.followup_round,
            status=result.status,
            jurisdiction=jurisdiction,
        )
        self.store.add_turn(
            run.session.id,
            turn_id=result.turn_id,
            user_message=message,
            facts=facts,
            rule_matches=rule_matches,
            response=result.public_payload(),
            provider_name=result.provider_name,
            provider_model=result.provider_model,
            provider_request_id=result.provider_request_id,
            usage=result.usage,
        )
        run.finish()
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
        return result

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


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 100:
        raise RequestInputError("jurisdiction 长度不能超过 100")
    return normalized
