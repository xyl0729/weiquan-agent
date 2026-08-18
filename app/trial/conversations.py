from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from app.agent.errors import (
    CaseNoProgressError,
    ConsultationConflictError,
    SessionNotFoundError,
)
from app.agent.progression import comparison_is_equivalent
from app.db.contracts import ConsultationCommitCommand
from app.db.models import SessionRecord, TurnRecord


@dataclass(slots=True)
class _TrialConversation:
    session: SessionRecord
    turns: deque[TurnRecord]


class InMemoryTrialConversationStore:
    """Bounded, process-local conversation context for anonymous trials."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        capacity: int = 256,
        max_turns: int = 6,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        self.ttl = timedelta(seconds=ttl_seconds)
        self.capacity = capacity
        self.max_turns = max_turns
        self._now = now or (lambda: datetime.now(UTC))
        self._conversations: OrderedDict[str, _TrialConversation] = (
            OrderedDict()
        )
        self._lock = RLock()

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
        normalized_owner_id = self._uuid(owner_id)
        normalized_session_id = self._uuid(session_id)
        session = SessionRecord(
            id=normalized_session_id,
            owner_id=normalized_owner_id,
            scenario_id=scenario_id,
            facts=dict(facts or {}),
            followup_round=0,
            status="collecting",
            jurisdiction=jurisdiction,
            created_at=current,
            updated_at=current,
            expires_at=current + self.ttl,
        )
        with self._lock:
            self._purge_expired(current)
            if normalized_session_id in self._conversations:
                raise ConsultationConflictError()
            while len(self._conversations) >= self.capacity:
                self._conversations.popitem(last=False)
            self._conversations[normalized_session_id] = _TrialConversation(
                session=session,
                turns=deque(maxlen=self.max_turns),
            )
        return session

    def require_session(
        self,
        session_id: str,
        *,
        owner_id: str,
        now: datetime | None = None,
    ) -> SessionRecord:
        normalized_session_id = self._uuid(session_id)
        normalized_owner_id = self._uuid(owner_id)
        current = self._utc(now)
        with self._lock:
            self._purge_expired(current)
            conversation = self._conversations.get(normalized_session_id)
            if (
                conversation is None
                or conversation.session.owner_id != normalized_owner_id
            ):
                raise SessionNotFoundError()
            return conversation.session

    def list_turns(
        self,
        session_id: str,
        *,
        owner_id: str,
    ) -> list[TurnRecord]:
        normalized_session_id = self._uuid(session_id)
        normalized_owner_id = self._uuid(owner_id)
        current = self._utc()
        with self._lock:
            self._purge_expired(current)
            conversation = self._conversations.get(normalized_session_id)
            if (
                conversation is None
                or conversation.session.owner_id != normalized_owner_id
            ):
                raise SessionNotFoundError()
            return list(conversation.turns)

    def persist_session_turn(
        self,
        command: ConsultationCommitCommand,
    ) -> TurnRecord:
        if command.attachment_binding is not None:
            raise ValueError(
                "Anonymous trial conversations do not support attachments"
            )
        current = self._utc(command.occurred_at)
        owner_id = self._uuid(command.owner_id)
        session_id = self._uuid(command.session_id)
        turn_id = self._uuid(command.turn.turn_id)

        with self._lock:
            self._purge_expired(current)
            conversation = self._conversations.get(session_id)
            if (
                conversation is None
                or conversation.session.owner_id != owner_id
            ):
                raise SessionNotFoundError()

            latest = (
                conversation.turns[-1]
                if conversation.turns
                else None
            )
            self._recheck_commit(command, latest)
            if any(turn.id == turn_id for turn in conversation.turns):
                raise ConsultationConflictError()

            updated_session = SessionRecord(
                id=session_id,
                owner_id=owner_id,
                scenario_id=command.session.scenario_id,
                facts=dict(command.session.facts),
                followup_round=command.session.followup_round,
                status=command.session.status,
                jurisdiction=command.session.jurisdiction,
                created_at=conversation.session.created_at,
                updated_at=current,
                expires_at=current + self.ttl,
            )
            turn = TurnRecord(
                id=turn_id,
                owner_id=owner_id,
                session_id=session_id,
                user_message=command.turn.user_message.strip(),
                facts=dict(command.turn.facts),
                rule_matches=[
                    dict(item) for item in command.turn.rule_matches
                ],
                response=dict(command.turn.response),
                provider_name=command.turn.provider_name,
                provider_model=command.turn.provider_model,
                provider_request_id=command.turn.provider_request_id,
                usage=command.turn.usage,
                created_at=current,
            )

            conversation.session = updated_session
            conversation.turns.append(turn)
            self._conversations.move_to_end(session_id)
            return turn

    def _recheck_commit(
        self,
        command: ConsultationCommitCommand,
        latest: TurnRecord | None,
    ) -> None:
        guard_enabled = bool(command.comparison_units) or (
            command.expected_latest_turn_id is not None
        )
        if not guard_enabled:
            return

        latest_id = latest.id if latest is not None else None
        expected_id = (
            self._uuid(command.expected_latest_turn_id)
            if command.expected_latest_turn_id is not None
            else None
        )
        equivalent = bool(
            latest is not None
            and command.comparison_units
            and comparison_is_equivalent(
                command.comparison_units,
                latest.response,
            )
        )
        if latest_id != expected_id:
            if equivalent:
                raise CaseNoProgressError()
            raise ConsultationConflictError()
        if equivalent:
            raise CaseNoProgressError()

    def _purge_expired(self, current: datetime) -> None:
        expired = [
            session_id
            for session_id, conversation in self._conversations.items()
            if conversation.session.expires_at <= current
        ]
        for session_id in expired:
            self._conversations.pop(session_id, None)

    def _utc(self, value: datetime | None = None) -> datetime:
        current = value if value is not None else self._now()
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current.astimezone(UTC)

    @staticmethod
    def _uuid(value: object | None) -> str:
        if value is None:
            return str(uuid4())
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Expected a valid UUID") from exc
