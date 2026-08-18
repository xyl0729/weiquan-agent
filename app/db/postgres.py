from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.engine import Engine, RowMapping

from app.agent.errors import (
    CaseNoProgressError,
    ConsultationConflictError,
    SessionNotFoundError,
)
from app.agent.models import UsageInfo
from app.attachments.errors import (
    AttachmentErrorCode,
    AttachmentInputError,
    AttachmentNotFoundError,
    AttachmentResourceLimitError,
    AttachmentStateConflictError,
)
from app.attachments.models import (
    AttachmentMediaType,
    ExtractionResult,
    normalize_confirmed_text,
)
from app.db.contracts import (
    AttachmentBindingCommand,
    ConsultationCommitCommand,
)
from app.db.models import (
    AttachmentRecord,
    AuditRecord,
    SessionHistoryRecord,
    SessionHistoryTurnRecord,
    SessionListRecord,
    SessionRecord,
    TurnRecord,
)
from app.db.tables import (
    consultation_attachments,
    consultation_deletion_outbox,
    consultation_sessions,
    consultation_turns,
    content_audit_records,
)
from app.deletion.service import DeletionIntent


_SECRET_PATTERN = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer\s+|bearer\s+)?"
    r"(sk-[A-Za-z0-9_-]{12,})"
)
_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "headers",
    "httpheaders",
    "messages",
    "prompt",
    "rawprompt",
    "requestbody",
    "requestheaders",
    "secret",
    "token",
}
_PROCESSING_FAILURE_CODES = frozenset(
    {
        "attachment_type_unsupported",
        "attachment_type_mismatch",
        "attachment_name_invalid",
        "attachment_pdf_encrypted",
        "attachment_corrupt",
        "attachment_text_empty",
        "attachment_too_large",
        "attachment_page_limit_exceeded",
        "attachment_pixel_limit_exceeded",
        "attachment_extracted_text_too_long",
        "attachment_extraction_timeout",
        "attachment_service_busy",
        "attachment_service_unavailable",
    }
)


class PostgresApplicationStore:
    def __init__(
        self,
        engine: Engine,
        *,
        retention_days: int = 30,
        attachment_draft_ttl_seconds: int = 3600,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days 必须大于 0")
        if attachment_draft_ttl_seconds <= 0:
            raise ValueError("attachment_draft_ttl_seconds 必须大于 0")
        self.engine = engine
        self.retention = timedelta(days=retention_days)
        self.attachment_draft_ttl = timedelta(
            seconds=attachment_draft_ttl_seconds
        )
        self._now = now or (lambda: datetime.now(UTC))

    def create_session(
        self,
        *,
        owner_id: str,
        scenario_id: str | None = None,
        facts: Mapping[str, Any] | None = None,
        jurisdiction: str | None = None,
        now: datetime | None = None,
        session_id: str | None = None,
    ) -> SessionRecord:
        current = self._utc(now)
        normalized_owner_id = _uuid(owner_id)
        normalized_session_id = _uuid(session_id)
        safe_facts = _safe_mapping(facts or {})
        values = {
            "id": normalized_session_id,
            "owner_id": normalized_owner_id,
            "scenario_id": _optional_text(scenario_id, 100),
            "facts": safe_facts,
            "followup_round": 0,
            "status": "collecting",
            "jurisdiction": _optional_text(jurisdiction, 100),
            "created_at": current,
            "updated_at": current,
            "expires_at": current + self.retention,
        }
        with self.engine.begin() as connection:
            connection.execute(insert(consultation_sessions).values(**values))
            row = connection.execute(
                select(consultation_sessions).where(
                    consultation_sessions.c.id == normalized_session_id,
                    consultation_sessions.c.owner_id
                    == normalized_owner_id,
                )
            ).mappings().one()
        return _session_from_row(row)

    def get_session(
        self,
        session_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> SessionRecord | None:
        normalized_session_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(consultation_sessions).where(
                    consultation_sessions.c.id == normalized_session_id,
                    consultation_sessions.c.owner_id
                    == normalized_owner_id,
                    consultation_sessions.c.expires_at > current,
                    consultation_sessions.c.deleted_at.is_(None),
                )
            ).mappings().one_or_none()
        return _session_from_row(row) if row is not None else None

    def require_session(
        self,
        session_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> SessionRecord:
        session = self.get_session(
            session_id,
            owner_id=owner_id,
            now=now,
        )
        if session is None:
            raise SessionNotFoundError()
        return session

    def list_sessions(
        self,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> list[SessionListRecord]:
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        first_message = (
            select(consultation_turns.c.user_message)
            .where(
                consultation_turns.c.owner_id
                == consultation_sessions.c.owner_id,
                consultation_turns.c.session_id
                == consultation_sessions.c.id,
            )
            .order_by(
                consultation_turns.c.created_at,
                consultation_turns.c.id,
            )
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            select(
                consultation_sessions.c.id,
                consultation_sessions.c.owner_id,
                consultation_sessions.c.scenario_id,
                consultation_sessions.c.status,
                consultation_sessions.c.created_at,
                consultation_sessions.c.updated_at,
                consultation_sessions.c.expires_at,
                first_message.label("first_user_message"),
            )
            .where(
                consultation_sessions.c.owner_id == normalized_owner_id,
                consultation_sessions.c.expires_at > current,
                consultation_sessions.c.deleted_at.is_(None),
                first_message.is_not(None),
            )
            .order_by(
                consultation_sessions.c.updated_at.desc(),
                consultation_sessions.c.id,
            )
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_session_list_from_row(row) for row in rows]

    def delete_session(
        self,
        session_id: str,
        *,
        owner_id: str,
    ) -> bool:
        normalized_session_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        with self.engine.begin() as connection:
            result = connection.execute(
                delete(consultation_sessions).where(
                    consultation_sessions.c.id == normalized_session_id,
                    consultation_sessions.c.owner_id
                    == normalized_owner_id,
                    consultation_sessions.c.deleted_at.is_(None),
                )
            )
        return bool(result.rowcount)

    def begin_session_deletion(
        self,
        session_id: str,
        *,
        owner_id: str,
        deleted_at: datetime,
    ) -> DeletionIntent | None:
        normalized_session_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(deleted_at)
        with self.engine.begin() as connection:
            session_row = connection.execute(
                select(
                    consultation_sessions.c.id,
                    consultation_sessions.c.deleted_at,
                )
                .where(
                    consultation_sessions.c.id == normalized_session_id,
                    consultation_sessions.c.owner_id
                    == normalized_owner_id,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if session_row is None:
                return None

            existing_deleted_at = session_row["deleted_at"]
            if existing_deleted_at is None:
                result = connection.execute(
                    update(consultation_sessions)
                    .where(
                        consultation_sessions.c.id
                        == normalized_session_id,
                        consultation_sessions.c.owner_id
                        == normalized_owner_id,
                        consultation_sessions.c.deleted_at.is_(None),
                    )
                    .values(deleted_at=current)
                )
                if result.rowcount != 1:
                    raise RuntimeError("删除意图写入失败")
                connection.execute(
                    insert(consultation_deletion_outbox).values(
                        session_id=normalized_session_id,
                        deleted_at=current,
                        manifest_uploaded_at=None,
                        completed_at=None,
                        last_attempted_at=current,
                        last_error_category=None,
                    )
                )
            outbox_row = connection.execute(
                select(consultation_deletion_outbox)
                .where(
                    consultation_deletion_outbox.c.session_id
                    == normalized_session_id,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if outbox_row is None:
                raise RuntimeError("删除意图状态不完整")
        return _deletion_intent_from_row(outbox_row)

    def mark_deletion_manifest_uploaded(
        self,
        session_id: str,
        *,
        deleted_at: datetime,
        uploaded_at: datetime,
    ) -> DeletionIntent:
        normalized_session_id = _uuid(session_id)
        normalized_deleted_at = self._utc(deleted_at)
        normalized_uploaded_at = self._utc(uploaded_at)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(consultation_deletion_outbox)
                .where(
                    consultation_deletion_outbox.c.session_id
                    == normalized_session_id,
                    consultation_deletion_outbox.c.deleted_at
                    == normalized_deleted_at,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if row is None:
                raise RuntimeError("删除意图不存在")
            if row["manifest_uploaded_at"] is None:
                connection.execute(
                    update(consultation_deletion_outbox)
                    .where(
                        consultation_deletion_outbox.c.session_id
                        == normalized_session_id,
                        consultation_deletion_outbox.c.deleted_at
                        == normalized_deleted_at,
                        consultation_deletion_outbox.c.completed_at.is_(
                            None
                        ),
                    )
                    .values(
                        manifest_uploaded_at=normalized_uploaded_at,
                        last_attempted_at=normalized_uploaded_at,
                        last_error_category=None,
                    )
                )
                row = connection.execute(
                    select(consultation_deletion_outbox).where(
                        consultation_deletion_outbox.c.session_id
                        == normalized_session_id,
                    )
                ).mappings().one()
        return _deletion_intent_from_row(row)

    def complete_session_deletion(
        self,
        session_id: str,
        *,
        deleted_at: datetime,
        completed_at: datetime,
    ) -> bool:
        normalized_session_id = _uuid(session_id)
        normalized_deleted_at = self._utc(deleted_at)
        normalized_completed_at = self._utc(completed_at)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(consultation_deletion_outbox)
                .where(
                    consultation_deletion_outbox.c.session_id
                    == normalized_session_id,
                    consultation_deletion_outbox.c.deleted_at
                    == normalized_deleted_at,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if row is None or row["manifest_uploaded_at"] is None:
                return False
            if row["completed_at"] is not None:
                return True

            connection.execute(
                delete(consultation_sessions).where(
                    consultation_sessions.c.id == normalized_session_id,
                    consultation_sessions.c.deleted_at
                    == normalized_deleted_at,
                )
            )
            result = connection.execute(
                update(consultation_deletion_outbox)
                .where(
                    consultation_deletion_outbox.c.session_id
                    == normalized_session_id,
                    consultation_deletion_outbox.c.deleted_at
                    == normalized_deleted_at,
                    consultation_deletion_outbox.c.completed_at.is_(None),
                )
                .values(
                    completed_at=normalized_completed_at,
                    last_attempted_at=normalized_completed_at,
                    last_error_category=None,
                )
            )
        return result.rowcount == 1

    def record_deletion_failure(
        self,
        session_id: str,
        *,
        category: str,
        attempted_at: datetime,
    ) -> None:
        normalized_category = _required_text(category, 50)
        if normalized_category not in {
            "encryption_failed",
            "upload_failed",
            "storage_failed",
        }:
            raise ValueError("未知删除失败类别")
        with self.engine.begin() as connection:
            connection.execute(
                update(consultation_deletion_outbox)
                .where(
                    consultation_deletion_outbox.c.session_id
                    == _uuid(session_id),
                    consultation_deletion_outbox.c.completed_at.is_(None),
                )
                .values(
                    last_attempted_at=self._utc(attempted_at),
                    last_error_category=normalized_category,
                )
            )

    def list_pending_deletions(
        self,
        *,
        limit: int,
    ) -> list[DeletionIntent]:
        bounded = int(limit)
        if not 1 <= bounded <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(consultation_deletion_outbox)
                .where(
                    consultation_deletion_outbox.c.completed_at.is_(None)
                )
                .order_by(
                    consultation_deletion_outbox.c.deleted_at,
                    consultation_deletion_outbox.c.session_id,
                )
                .limit(bounded)
            ).mappings().all()
        return [_deletion_intent_from_row(row) for row in rows]

    def get_session_history(
        self,
        session_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> SessionHistoryRecord | None:
        normalized_session_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self.engine.begin() as connection:
            session_row = connection.execute(
                select(consultation_sessions)
                .where(
                    consultation_sessions.c.id
                    == normalized_session_id,
                    consultation_sessions.c.owner_id
                    == normalized_owner_id,
                    consultation_sessions.c.expires_at > current,
                    consultation_sessions.c.deleted_at.is_(None),
                )
                .with_for_update()
            ).mappings().one_or_none()
            if session_row is None:
                return None
            turn_rows = connection.execute(
                select(consultation_turns)
                .where(
                    consultation_turns.c.session_id
                    == normalized_session_id,
                    consultation_turns.c.owner_id
                    == normalized_owner_id,
                )
                .order_by(
                    consultation_turns.c.created_at,
                    consultation_turns.c.id,
                )
            ).mappings().all()
            attachment_rows = connection.execute(
                select(consultation_attachments)
                .where(
                    consultation_attachments.c.session_id
                    == normalized_session_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    consultation_attachments.c.status == "bound",
                )
                .order_by(
                    consultation_attachments.c.turn_id,
                    consultation_attachments.c.turn_position,
                    consultation_attachments.c.id,
                )
            ).mappings().all()

        session = _session_from_row(session_row)
        turns = tuple(_turn_from_row(row) for row in turn_rows)
        turn_ids = {turn.id for turn in turns}
        attachments_by_turn: dict[str, list[AttachmentRecord]] = {
            turn.id: [] for turn in turns
        }
        for row in attachment_rows:
            attachment = _attachment_from_row(row)
            if (
                attachment.session_id != session.id
                or attachment.turn_id not in turn_ids
            ):
                raise ValueError("历史附件关系无效")
            attachments_by_turn[attachment.turn_id].append(attachment)

        history_turns: list[SessionHistoryTurnRecord] = []
        for turn in turns:
            turn_attachments = tuple(attachments_by_turn[turn.id])
            positions = tuple(
                attachment.turn_position
                for attachment in turn_attachments
            )
            if (
                len(turn_attachments) > 3
                or positions != tuple(range(len(turn_attachments)))
            ):
                raise ValueError("历史附件顺序无效")
            history_turns.append(
                SessionHistoryTurnRecord(
                    turn=turn,
                    attachments=turn_attachments,
                )
            )
        return SessionHistoryRecord(
            session=session,
            turns=tuple(history_turns),
        )

    def persist_session_turn(
        self,
        command: ConsultationCommitCommand,
    ) -> TurnRecord:
        owner_id = _uuid(command.owner_id)
        session_id = _uuid(command.session_id)
        turn_id = _uuid(command.turn.turn_id)
        current = self._utc(command.occurred_at)
        followup_round = int(command.session.followup_round)
        if not 0 <= followup_round <= 2:
            raise ValueError("followup_round 必须在 0 到 2 之间")
        status = _session_status(command.session.status)
        session_facts = _safe_mapping(command.session.facts)
        turn_facts = _safe_mapping(command.turn.facts)
        rule_matches = [
            _safe_mapping(item)
            for item in command.turn.rule_matches
        ]
        response = _safe_mapping(command.turn.response)
        message = _SECRET_PATTERN.sub(
            "[REDACTED]",
            command.turn.user_message.strip(),
        )
        if not message:
            raise ValueError("user_message 不能为空")

        with self.engine.begin() as connection:
            active = connection.execute(
                select(consultation_sessions.c.id)
                .where(
                    consultation_sessions.c.id == session_id,
                    consultation_sessions.c.owner_id == owner_id,
                    consultation_sessions.c.expires_at > current,
                    consultation_sessions.c.deleted_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if active is None:
                raise SessionNotFoundError()

            latest = connection.execute(
                select(
                    consultation_turns.c.id,
                    consultation_turns.c.response,
                )
                .where(
                    consultation_turns.c.session_id == session_id,
                    consultation_turns.c.owner_id == owner_id,
                )
                .order_by(
                    consultation_turns.c.created_at.desc(),
                    consultation_turns.c.id.desc(),
                )
                .limit(1)
            ).mappings().one_or_none()
            _recheck_consultation_commit(command, latest)

            result = connection.execute(
                update(consultation_sessions)
                .where(
                    consultation_sessions.c.id == session_id,
                    consultation_sessions.c.owner_id == owner_id,
                    consultation_sessions.c.deleted_at.is_(None),
                )
                .values(
                    scenario_id=_optional_text(
                        command.session.scenario_id,
                        100,
                    ),
                    facts=session_facts,
                    followup_round=followup_round,
                    status=status,
                    jurisdiction=_optional_text(
                        command.session.jurisdiction,
                        100,
                    ),
                    updated_at=current,
                    expires_at=current + self.retention,
                )
            )
            if result.rowcount != 1:
                raise SessionNotFoundError()

            usage = command.turn.usage
            connection.execute(
                insert(consultation_turns).values(
                    id=turn_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    user_message=message,
                    facts=turn_facts,
                    rule_matches=rule_matches,
                    response=response,
                    provider_name=_optional_text(
                        command.turn.provider_name,
                        50,
                    ),
                    provider_model=_optional_text(
                        command.turn.provider_model,
                        200,
                    ),
                    provider_request_id=_optional_text(
                        command.turn.provider_request_id,
                        200,
                    ),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    estimated_cost_usd=usage.estimated_cost_usd,
                    created_at=current,
                )
            )
            if command.attachment_binding is not None:
                self._bind_reserved_attachments(
                    connection,
                    command.attachment_binding,
                    owner_id=owner_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    now=current,
                )
            row = connection.execute(
                select(consultation_turns).where(
                    consultation_turns.c.id == turn_id,
                    consultation_turns.c.owner_id == owner_id,
                )
            ).mappings().one()
        return _turn_from_row(row)

    def list_turns(
        self,
        session_id: str,
        *,
        owner_id: str,
    ) -> list[TurnRecord]:
        normalized_session_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(None)
        with self.engine.begin() as connection:
            active = connection.execute(
                select(consultation_sessions.c.id)
                .where(
                    consultation_sessions.c.id
                    == normalized_session_id,
                    consultation_sessions.c.owner_id
                    == normalized_owner_id,
                    consultation_sessions.c.expires_at > current,
                    consultation_sessions.c.deleted_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if active is None:
                return []
            rows = connection.execute(
                select(consultation_turns)
                .where(
                    consultation_turns.c.session_id
                    == normalized_session_id,
                    consultation_turns.c.owner_id
                    == normalized_owner_id,
                )
                .order_by(
                    consultation_turns.c.created_at,
                    consultation_turns.c.id,
                )
            ).mappings().all()
        return [_turn_from_row(row) for row in rows]

    def create_processing(
        self,
        *,
        owner_id: str,
        original_name: str,
        media_type: AttachmentMediaType,
        size_bytes: int,
        sha256: str,
        now: datetime | None = None,
        attachment_id: str | None = None,
    ) -> AttachmentRecord:
        current = self._utc(now)
        record = AttachmentRecord(
            id=_uuid(attachment_id),
            owner_id=_uuid(owner_id),
            status="processing",
            original_name=original_name,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha256.lower(),
            created_at=current,
            updated_at=current,
            expires_at=current + self.attachment_draft_ttl,
        )
        with self.engine.begin() as connection:
            connection.execute(
                insert(consultation_attachments).values(
                    id=record.id,
                    owner_id=record.owner_id,
                    session_id=None,
                    turn_id=None,
                    turn_position=None,
                    status=record.status,
                    original_name=record.original_name,
                    media_type=record.media_type,
                    size_bytes=record.size_bytes,
                    sha256=record.sha256,
                    page_count=None,
                    extraction_method=None,
                    confirmed_text=None,
                    warnings=[],
                    error_code=None,
                    reservation_id=None,
                    reserved_at=None,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    expires_at=record.expires_at,
                )
            )
        return record

    def save_extraction(
        self,
        attachment_id: str,
        result: ExtractionResult,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord:
        normalized_id = _uuid(attachment_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self.engine.begin() as connection:
            record = self._require_attachment(
                connection,
                normalized_id,
                normalized_owner_id,
                for_update=True,
            )
            self._require_unexpired(record, current)
            if record.status == "bound":
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            if record.status != "processing":
                raise AttachmentStateConflictError(
                    "attachment_not_reviewable"
                )
            if result.media_type != record.media_type:
                raise AttachmentInputError("attachment_type_mismatch")
            connection.execute(
                update(consultation_attachments)
                .where(
                    consultation_attachments.c.id == normalized_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                )
                .values(
                    status="review_required",
                    page_count=result.page_count,
                    extraction_method=result.extraction_method,
                    confirmed_text=None,
                    warnings=list(result.warnings),
                    error_code=None,
                    updated_at=current,
                )
            )
            row = connection.execute(
                select(consultation_attachments).where(
                    consultation_attachments.c.id == normalized_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                )
            ).mappings().one()
        return _attachment_from_row(row)

    def save_failure(
        self,
        attachment_id: str,
        code: AttachmentErrorCode,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord:
        if code not in _PROCESSING_FAILURE_CODES:
            raise ValueError("错误代码不能作为附件处理失败结果")
        normalized_id = _uuid(attachment_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self.engine.begin() as connection:
            record = self._require_attachment(
                connection,
                normalized_id,
                normalized_owner_id,
                for_update=True,
            )
            self._require_unexpired(record, current)
            if record.status == "bound":
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            if record.status != "processing":
                raise AttachmentStateConflictError(
                    "attachment_not_reviewable"
                )
            connection.execute(
                update(consultation_attachments)
                .where(
                    consultation_attachments.c.id == normalized_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                )
                .values(
                    status="failed",
                    page_count=None,
                    extraction_method=None,
                    confirmed_text=None,
                    warnings=[],
                    error_code=code,
                    updated_at=current,
                )
            )
            row = connection.execute(
                select(consultation_attachments).where(
                    consultation_attachments.c.id == normalized_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                )
            ).mappings().one()
        return _attachment_from_row(row)

    def confirm(
        self,
        attachment_id: str,
        confirmed_text: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord:
        normalized_id = _uuid(attachment_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        normalized_text = normalize_confirmed_text(confirmed_text)
        assert normalized_text is not None
        with self.engine.begin() as connection:
            record = self._require_attachment(
                connection,
                normalized_id,
                normalized_owner_id,
                for_update=True,
            )
            self._require_unexpired(record, current)
            if record.status == "bound" or record.reservation_id is not None:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            if record.status not in {"review_required", "confirmed"}:
                raise AttachmentStateConflictError(
                    "attachment_not_reviewable"
                )
            connection.execute(
                update(consultation_attachments)
                .where(
                    consultation_attachments.c.id == normalized_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                )
                .values(
                    status="confirmed",
                    confirmed_text=normalized_text,
                    updated_at=current,
                )
            )
            row = connection.execute(
                select(consultation_attachments).where(
                    consultation_attachments.c.id == normalized_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                )
            ).mappings().one()
        return _attachment_from_row(row)

    def get(
        self,
        attachment_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord:
        record = self.get_optional(
            attachment_id,
            owner_id=owner_id,
            now=now,
        )
        if record is None:
            raise AttachmentNotFoundError()
        return record

    def get_optional(
        self,
        attachment_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord | None:
        normalized_id = _uuid(attachment_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(consultation_attachments).where(
                    consultation_attachments.c.id == normalized_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    _attachment_is_visible(current),
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        record = _attachment_from_row(row)
        if record.expires_at is not None and record.expires_at <= current:
            return None
        return record

    def reserve(
        self,
        attachment_ids: Sequence[str],
        *,
        owner_id: str,
        reservation_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        normalized_ids = _attachment_ids(attachment_ids)
        normalized_owner_id = _uuid(owner_id)
        normalized_reservation_id = _uuid(reservation_id)
        current = self._utc(now)
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(consultation_attachments.c.id)
                .where(
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    consultation_attachments.c.reservation_id
                    == normalized_reservation_id,
                )
                .limit(1)
                .with_for_update()
            ).first()
            if existing is not None:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            rows = connection.execute(
                select(consultation_attachments)
                .where(
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    consultation_attachments.c.id.in_(normalized_ids),
                )
                .with_for_update()
            ).mappings().all()
            by_id = {
                str(row["id"]): _attachment_from_row(row)
                for row in rows
            }
            if any(
                attachment_id not in by_id
                for attachment_id in normalized_ids
            ):
                raise AttachmentNotFoundError()
            for attachment_id in normalized_ids:
                record = by_id[attachment_id]
                self._require_unexpired(record, current)
                if (
                    record.status == "bound"
                    or record.reservation_id is not None
                ):
                    raise AttachmentStateConflictError(
                        "attachment_already_bound"
                    )
                if record.status != "confirmed":
                    raise AttachmentStateConflictError(
                        "attachment_not_confirmed"
                    )
            for position, attachment_id in enumerate(normalized_ids):
                result = connection.execute(
                    update(consultation_attachments)
                    .where(
                        consultation_attachments.c.id == attachment_id,
                        consultation_attachments.c.owner_id
                        == normalized_owner_id,
                        consultation_attachments.c.status == "confirmed",
                        consultation_attachments.c.reservation_id.is_(None),
                        consultation_attachments.c.session_id.is_(None),
                        consultation_attachments.c.turn_id.is_(None),
                    )
                    .values(
                        reservation_id=normalized_reservation_id,
                        reserved_at=current,
                        turn_position=position,
                        updated_at=current,
                    )
                )
                if result.rowcount != 1:
                    raise AttachmentStateConflictError(
                        "attachment_already_bound"
                    )
        return normalized_reservation_id

    def release(
        self,
        reservation_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> int:
        normalized_reservation_id = _uuid(reservation_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(consultation_attachments)
                .where(
                    consultation_attachments.c.reservation_id
                    == normalized_reservation_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    consultation_attachments.c.status == "confirmed",
                    consultation_attachments.c.session_id.is_(None),
                    consultation_attachments.c.turn_id.is_(None),
                )
                .values(
                    reservation_id=None,
                    reserved_at=None,
                    turn_position=None,
                    updated_at=current,
                )
            )
        return max(result.rowcount or 0, 0)

    def bind_reserved(
        self,
        reservation_id: str,
        *,
        owner_id: str,
        session_id: str,
        turn_id: str,
        expected_ids: Sequence[str],
        now: datetime | None = None,
    ) -> list[AttachmentRecord]:
        normalized_owner_id = _uuid(owner_id)
        normalized_session_id = _uuid(session_id)
        normalized_turn_id = _uuid(turn_id)
        normalized_ids = _attachment_ids(expected_ids)
        current = self._utc(now)
        with self.engine.begin() as connection:
            self._bind_reserved_attachments(
                connection,
                AttachmentBindingCommand(
                    reservation_id=_uuid(reservation_id),
                    attachment_ids=normalized_ids,
                ),
                owner_id=normalized_owner_id,
                session_id=normalized_session_id,
                turn_id=normalized_turn_id,
                now=current,
            )
            rows = connection.execute(
                select(consultation_attachments)
                .where(
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    consultation_attachments.c.turn_id
                    == normalized_turn_id,
                )
                .order_by(
                    consultation_attachments.c.turn_position,
                    consultation_attachments.c.id,
                )
            ).mappings().all()
        return [_attachment_from_row(row) for row in rows]

    def list_for_turn(
        self,
        turn_id: str,
        *,
        owner_id: str,
    ) -> list[AttachmentRecord]:
        normalized_turn_id = _uuid(turn_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(None)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(consultation_attachments)
                .join(
                    consultation_sessions,
                    and_(
                        consultation_sessions.c.id
                        == consultation_attachments.c.session_id,
                        consultation_sessions.c.owner_id
                        == consultation_attachments.c.owner_id,
                    ),
                )
                .where(
                    consultation_attachments.c.turn_id
                    == normalized_turn_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    consultation_attachments.c.status == "bound",
                    consultation_sessions.c.expires_at > current,
                    consultation_sessions.c.deleted_at.is_(None),
                )
                .order_by(
                    consultation_attachments.c.turn_position,
                    consultation_attachments.c.id,
                )
            ).mappings().all()
        return [_attachment_from_row(row) for row in rows]

    def delete(
        self,
        attachment_id: str,
        *,
        owner_id: str,
    ) -> None:
        normalized_id = _uuid(attachment_id)
        normalized_owner_id = _uuid(owner_id)
        with self.engine.begin() as connection:
            record = self._require_attachment(
                connection,
                normalized_id,
                normalized_owner_id,
                for_update=True,
            )
            if record.status == "bound" or record.reservation_id is not None:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            connection.execute(
                delete(consultation_attachments).where(
                    consultation_attachments.c.id == normalized_id,
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                )
            )

    def purge_expired(
        self,
        *,
        owner_id: str,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        targets = (
            select(consultation_attachments.c.id)
            .where(
                consultation_attachments.c.owner_id
                == normalized_owner_id,
                consultation_attachments.c.session_id.is_(None),
                consultation_attachments.c.expires_at <= current,
            )
            .order_by(
                consultation_attachments.c.expires_at,
                consultation_attachments.c.id,
            )
            .limit(int(limit))
        )
        with self.engine.begin() as connection:
            result = connection.execute(
                delete(consultation_attachments).where(
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    consultation_attachments.c.id.in_(targets),
                )
            )
        return max(result.rowcount or 0, 0)

    def fail_stale_processing(
        self,
        *,
        owner_id: str,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        targets = (
            select(consultation_attachments.c.id)
            .where(
                consultation_attachments.c.owner_id
                == normalized_owner_id,
                consultation_attachments.c.status.in_(
                    ("processing", "review_required")
                ),
            )
            .order_by(
                consultation_attachments.c.created_at,
                consultation_attachments.c.id,
            )
            .limit(int(limit))
        )
        with self.engine.begin() as connection:
            result = connection.execute(
                update(consultation_attachments)
                .where(
                    consultation_attachments.c.owner_id
                    == normalized_owner_id,
                    consultation_attachments.c.id.in_(targets),
                )
                .values(
                    status="failed",
                    page_count=None,
                    extraction_method=None,
                    confirmed_text=None,
                    warnings=[],
                    error_code="attachment_service_unavailable",
                    updated_at=current,
                )
            )
        return max(result.rowcount or 0, 0)

    def add_audit_record(
        self,
        session_id: str,
        *,
        owner_id: str,
        stage: str,
        status: str,
        audit_id: str | None = None,
        turn_id: str | None = None,
        duration_ms: int = 0,
        playbook_id: str | None = None,
        playbook_version: str | None = None,
        citations: Sequence[str] = (),
        error_category: str | None = None,
        now: datetime | None = None,
        record_id: str | None = None,
    ) -> AuditRecord:
        owner_id = _uuid(owner_id)
        session_id = _uuid(session_id)
        normalized_turn_id = _uuid(turn_id) if turn_id else None
        current = self._utc(now)
        if duration_ms < 0:
            raise ValueError("duration_ms 不能小于 0")
        values = {
            "id": _uuid(record_id),
            "owner_id": owner_id,
            "audit_id": _uuid(audit_id),
            "session_id": session_id,
            "turn_id": normalized_turn_id,
            "stage": _required_text(stage, 100),
            "status": _audit_status(status),
            "duration_ms": duration_ms,
            "playbook_id": _optional_text(playbook_id, 100),
            "playbook_version": _optional_text(playbook_version, 50),
            "citations": [
                _required_text(citation, 200)
                for citation in citations
            ],
            "error_category": _optional_text(error_category, 100),
            "created_at": current,
        }
        with self.engine.begin() as connection:
            active = connection.execute(
                select(consultation_sessions.c.id).where(
                    consultation_sessions.c.id == session_id,
                    consultation_sessions.c.owner_id == owner_id,
                    consultation_sessions.c.expires_at > current,
                    consultation_sessions.c.deleted_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if active is None:
                raise SessionNotFoundError()
            connection.execute(
                insert(content_audit_records).values(**values)
            )
            row = connection.execute(
                select(content_audit_records).where(
                    content_audit_records.c.id == values["id"],
                    content_audit_records.c.owner_id == owner_id,
                )
            ).mappings().one()
        return _audit_from_row(row)

    def _bind_reserved_attachments(
        self,
        connection: Any,
        command: AttachmentBindingCommand,
        *,
        owner_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> None:
        active = connection.execute(
            select(consultation_sessions.c.id)
            .where(
                consultation_sessions.c.id == session_id,
                consultation_sessions.c.owner_id == owner_id,
                consultation_sessions.c.expires_at > now,
                consultation_sessions.c.deleted_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if active is None:
            raise SessionNotFoundError()

        reservation_id = _uuid(command.reservation_id)
        attachment_ids = tuple(
            _uuid(attachment_id)
            for attachment_id in command.attachment_ids
        )
        if not 1 <= len(attachment_ids) <= 3:
            raise ValueError("每轮必须绑定一至三个附件")
        if len(attachment_ids) != len(set(attachment_ids)):
            raise ValueError("附件 ID 不得重复")

        existing = connection.execute(
            select(consultation_attachments.c.id).where(
                consultation_attachments.c.owner_id == owner_id,
                consultation_attachments.c.turn_id == turn_id,
            )
        ).first()
        if existing is not None:
            raise AttachmentStateConflictError(
                "attachment_already_bound"
            )
        rows = connection.execute(
            select(consultation_attachments)
            .where(
                consultation_attachments.c.owner_id == owner_id,
                consultation_attachments.c.reservation_id
                == reservation_id,
            )
            .order_by(
                consultation_attachments.c.turn_position,
                consultation_attachments.c.id,
            )
            .with_for_update()
        ).mappings().all()
        if tuple(str(row["id"]) for row in rows) != attachment_ids:
            raise AttachmentStateConflictError(
                "attachment_already_bound"
            )
        for position, row in enumerate(rows):
            expires_at = row["expires_at"]
            if (
                row["status"] != "confirmed"
                or row["turn_position"] != position
                or expires_at is None
                or expires_at <= now
            ):
                raise AttachmentStateConflictError(
                    "attachment_not_confirmed"
                )
            result = connection.execute(
                update(consultation_attachments)
                .where(
                    consultation_attachments.c.id == row["id"],
                    consultation_attachments.c.owner_id == owner_id,
                    consultation_attachments.c.status == "confirmed",
                    consultation_attachments.c.reservation_id
                    == reservation_id,
                    consultation_attachments.c.turn_position == position,
                )
                .values(
                    status="bound",
                    session_id=session_id,
                    turn_id=turn_id,
                    reservation_id=None,
                    reserved_at=None,
                    expires_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )

    @staticmethod
    def _require_attachment(
        connection: Any,
        attachment_id: str,
        owner_id: str,
        *,
        for_update: bool = False,
    ) -> AttachmentRecord:
        statement = select(consultation_attachments).where(
            consultation_attachments.c.id == attachment_id,
            consultation_attachments.c.owner_id == owner_id,
            _attachment_is_visible(),
        )
        if for_update:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise AttachmentNotFoundError()
        return _attachment_from_row(row)

    @staticmethod
    def _require_unexpired(
        record: AttachmentRecord,
        now: datetime,
    ) -> None:
        if record.expires_at is not None and record.expires_at <= now:
            raise AttachmentNotFoundError()

    def _utc(self, value: datetime | None) -> datetime:
        current = value or self._now()
        if current.tzinfo is None:
            raise ValueError("时间必须包含时区")
        return current.astimezone(UTC)


def _session_from_row(row: RowMapping) -> SessionRecord:
    return SessionRecord(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        scenario_id=row["scenario_id"],
        facts=dict(row["facts"]),
        followup_round=row["followup_round"],
        status=row["status"],
        jurisdiction=row["jurisdiction"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def _session_list_from_row(row: RowMapping) -> SessionListRecord:
    return SessionListRecord(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        scenario_id=row["scenario_id"],
        status=row["status"],
        first_user_message=row["first_user_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def _turn_from_row(row: RowMapping) -> TurnRecord:
    cost = row["estimated_cost_usd"]
    return TurnRecord(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        session_id=str(row["session_id"]),
        user_message=row["user_message"],
        facts=dict(row["facts"]),
        rule_matches=list(row["rule_matches"]),
        response=dict(row["response"]),
        provider_name=row["provider_name"],
        provider_model=row["provider_model"],
        provider_request_id=row["provider_request_id"],
        usage=UsageInfo(
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            estimated_cost_usd=(
                float(cost)
                if isinstance(cost, Decimal)
                else cost
            ),
        ),
        created_at=row["created_at"],
    )


def _audit_from_row(row: RowMapping) -> AuditRecord:
    return AuditRecord(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        audit_id=str(row["audit_id"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]) if row["turn_id"] else None,
        stage=row["stage"],
        status=row["status"],
        duration_ms=row["duration_ms"],
        playbook_id=row["playbook_id"],
        playbook_version=row["playbook_version"],
        citations=list(row["citations"]),
        error_category=row["error_category"],
        created_at=row["created_at"],
    )


def _attachment_from_row(row: RowMapping) -> AttachmentRecord:
    return AttachmentRecord(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        session_id=(
            str(row["session_id"]) if row["session_id"] else None
        ),
        turn_id=str(row["turn_id"]) if row["turn_id"] else None,
        turn_position=row["turn_position"],
        status=row["status"],
        original_name=row["original_name"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        page_count=row["page_count"],
        extraction_method=row["extraction_method"],
        extracted_blocks=(),
        confirmed_text=row["confirmed_text"],
        warnings=tuple(row["warnings"]),
        error_code=row["error_code"],
        reservation_id=(
            str(row["reservation_id"])
            if row["reservation_id"]
            else None
        ),
        reserved_at=row["reserved_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def _deletion_intent_from_row(row: RowMapping) -> DeletionIntent:
    return DeletionIntent(
        session_id=str(row["session_id"]),
        deleted_at=row["deleted_at"],
        manifest_uploaded_at=row["manifest_uploaded_at"],
        completed_at=row["completed_at"],
    )


def _attachment_is_visible(
    now: datetime | None = None,
) -> object:
    conditions = [
        consultation_sessions.c.id
        == consultation_attachments.c.session_id,
        consultation_sessions.c.owner_id
        == consultation_attachments.c.owner_id,
        consultation_sessions.c.deleted_at.is_(None),
    ]
    if now is not None:
        conditions.append(consultation_sessions.c.expires_at > now)
    active_session = select(consultation_sessions.c.id).where(
        *conditions
    )
    return or_(
        consultation_attachments.c.session_id.is_(None),
        active_session.exists(),
    )


def _attachment_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("附件 ID 必须是序列")
    normalized = tuple(_uuid(value) for value in values)
    if not normalized:
        raise ValueError("至少需要一个附件 ID")
    if len(normalized) > 3:
        raise AttachmentResourceLimitError("attachment_count_exceeded")
    if len(normalized) != len(set(normalized)):
        raise ValueError("附件 ID 不得重复")
    return normalized


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    _assert_safe_keys(normalized)
    return normalized


def _recheck_consultation_commit(
    command: ConsultationCommitCommand,
    latest: RowMapping | None,
) -> None:
    from app.agent.progression import comparison_is_equivalent

    guard_enabled = bool(command.comparison_units) or (
        command.expected_latest_turn_id is not None
    )
    if not guard_enabled:
        return

    latest_id = str(latest["id"]) if latest is not None else None
    expected_id = (
        _uuid(command.expected_latest_turn_id)
        if command.expected_latest_turn_id is not None
        else None
    )
    response = latest["response"] if latest is not None else None
    equivalent = bool(
        command.comparison_units
        and isinstance(response, Mapping)
        and comparison_is_equivalent(
            command.comparison_units,
            response,
        )
    )
    if latest_id != expected_id:
        if equivalent:
            raise CaseNoProgressError()
        raise ConsultationConflictError()
    if equivalent:
        raise CaseNoProgressError()


def _assert_safe_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _SENSITIVE_KEYS:
                raise ValueError(f"禁止持久化敏感字段: {key}")
            _assert_safe_keys(nested)
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            _assert_safe_keys(item)


def _uuid(value: object | None) -> str:
    if value is None:
        return str(uuid4())
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("ID 必须是有效 UUID") from exc


def _required_text(value: object, max_length: int) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("文本不能为空")
    if len(normalized) > max_length:
        raise ValueError(f"文本长度不能超过 {max_length}")
    return normalized


def _optional_text(value: object, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"文本长度不能超过 {max_length}")
    return normalized


def _session_status(value: object) -> str:
    normalized = _required_text(value, 30)
    if normalized not in {
        "collecting",
        "need_more_facts",
        "ready",
        "escalate",
        "error",
    }:
        raise ValueError("未知会话状态")
    return normalized


def _audit_status(value: object) -> str:
    normalized = _required_text(value, 30)
    if normalized not in {"started", "ok", "error", "degraded"}:
        raise ValueError("未知审计状态")
    return normalized
