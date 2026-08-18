from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.agent.models import UsageInfo
from app.attachments.errors import AttachmentErrorCode
from app.attachments.models import AttachmentMediaType, ExtractionResult
from app.db.models import (
    AttachmentRecord,
    AuditRecord,
    SessionHistoryRecord,
    SessionListRecord,
    SessionRecord,
    SessionStatus,
    TurnRecord,
)


LOCAL_DEVELOPMENT_OWNER_ID = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True, slots=True)
class SessionUpdateCommand:
    scenario_id: str | None
    facts: dict[str, Any]
    followup_round: int
    status: SessionStatus
    jurisdiction: str | None


@dataclass(frozen=True, slots=True)
class TurnWriteCommand:
    turn_id: str
    user_message: str
    facts: dict[str, Any]
    rule_matches: tuple[dict[str, Any], ...]
    response: dict[str, Any]
    provider_name: str | None = None
    provider_model: str | None = None
    provider_request_id: str | None = None
    usage: UsageInfo = field(default_factory=UsageInfo)


@dataclass(frozen=True, slots=True)
class AttachmentBindingCommand:
    reservation_id: str
    attachment_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConsultationCommitCommand:
    owner_id: str
    session_id: str
    session: SessionUpdateCommand
    turn: TurnWriteCommand
    attachment_binding: AttachmentBindingCommand | None = None
    occurred_at: datetime | None = None
    expected_latest_turn_id: str | None = None
    comparison_units: tuple[str, ...] = ()


@runtime_checkable
class ConsultationUnitOfWork(Protocol):
    def persist_session_turn(
        self,
        command: ConsultationCommitCommand,
    ) -> TurnRecord:
        ...


@runtime_checkable
class ConversationRepository(ConsultationUnitOfWork, Protocol):
    def create_session(
        self,
        *,
        owner_id: str,
        scenario_id: str | None = None,
        facts: Mapping[str, Any] | None = None,
        jurisdiction: str | None = None,
        now: datetime | None = None,
        session_id: str | None = None,
    ) -> SessionRecord: ...

    def require_session(
        self,
        session_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> SessionRecord: ...

    def list_turns(
        self,
        session_id: str,
        *,
        owner_id: str,
    ) -> list[TurnRecord]: ...


@runtime_checkable
class ConsultationRepository(ConversationRepository, Protocol):
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
    ) -> AuditRecord: ...


@runtime_checkable
class HistoryRepository(Protocol):
    def list_sessions(
        self,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> list[SessionListRecord]: ...

    def get_session_history(
        self,
        session_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> SessionHistoryRecord | None: ...

    def delete_session(
        self,
        session_id: str,
        *,
        owner_id: str,
    ) -> bool: ...


@runtime_checkable
class AttachmentRepository(Protocol):
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
    ) -> AttachmentRecord: ...

    def save_extraction(
        self,
        attachment_id: str,
        result: ExtractionResult,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord: ...

    def save_failure(
        self,
        attachment_id: str,
        code: AttachmentErrorCode,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord: ...

    def confirm(
        self,
        attachment_id: str,
        confirmed_text: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord: ...

    def get(
        self,
        attachment_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord: ...

    def get_optional(
        self,
        attachment_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> AttachmentRecord | None: ...

    def reserve(
        self,
        attachment_ids: Sequence[str],
        *,
        owner_id: str,
        reservation_id: str | None = None,
        now: datetime | None = None,
    ) -> str: ...

    def release(
        self,
        reservation_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> int: ...

    def bind_reserved(
        self,
        reservation_id: str,
        *,
        owner_id: str,
        session_id: str,
        turn_id: str,
        expected_ids: Sequence[str],
        now: datetime | None = None,
    ) -> list[AttachmentRecord]: ...

    def list_for_turn(
        self,
        turn_id: str,
        *,
        owner_id: str,
    ) -> list[AttachmentRecord]: ...

    def delete(
        self,
        attachment_id: str,
        *,
        owner_id: str,
    ) -> None: ...

    def purge_expired(
        self,
        *,
        owner_id: str,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int: ...

    def fail_stale_processing(
        self,
        *,
        owner_id: str,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int: ...


@runtime_checkable
class SessionRepository(
    ConsultationRepository,
    HistoryRepository,
    Protocol,
):
    pass


@runtime_checkable
class ApplicationStore(
    SessionRepository,
    AttachmentRepository,
    Protocol,
):
    pass
