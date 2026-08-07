from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from app.agent.errors import (
    DataIntegrityError,
    SessionNotFoundError,
    StorageUnavailableError,
)
from app.api.schemas import ConsultResponse
from app.db.models import SessionListRecord, SessionRecord, TurnRecord
from app.db.session import SessionStore


TITLE_LENGTH = 24
_TITLE_WHITESPACE = re.compile(r"\s+")
_PUBLIC_STATUSES = {"need_more_facts", "ready", "escalate"}


@dataclass(frozen=True, slots=True)
class HistorySession:
    session_id: str
    title: str
    scenario_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    turn_id: str
    user_message: str
    response: ConsultResponse
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionDetail:
    session: HistorySession
    turns: tuple[HistoryTurn, ...]


class SessionHistoryService:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def list_sessions(self) -> list[HistorySession]:
        try:
            records = self.store.list_sessions()
            return [_history_session(record) for record in records]
        except DataIntegrityError:
            raise
        except (ValidationError, ValueError) as exc:
            raise _history_integrity_error() from exc
        except (OSError, sqlite3.Error) as exc:
            raise StorageUnavailableError() from exc

    def get_session(self, session_id: str) -> SessionDetail:
        try:
            stored = self.store.get_session_history(session_id)
        except (ValidationError, ValueError) as exc:
            raise _history_integrity_error() from exc
        except (OSError, sqlite3.Error) as exc:
            raise StorageUnavailableError() from exc
        if stored is None:
            raise SessionNotFoundError()
        session, turns = stored
        if not turns:
            raise SessionNotFoundError()

        public_turns = tuple(_history_turn(turn) for turn in turns)
        return SessionDetail(
            session=_detail_session(session, turns[0]),
            turns=public_turns,
        )

    def delete_session(self, session_id: str) -> None:
        try:
            self.store.delete_session(session_id)
        except ValueError as exc:
            raise _history_integrity_error() from exc
        except (OSError, sqlite3.Error) as exc:
            raise StorageUnavailableError() from exc


def history_title(message: str) -> str:
    normalized = _TITLE_WHITESPACE.sub(" ", message).strip()
    if not normalized:
        raise ValueError("历史标题来源不能为空")
    if len(normalized) <= TITLE_LENGTH:
        return normalized
    return f"{normalized[:TITLE_LENGTH]}…"


def _history_session(record: SessionListRecord) -> HistorySession:
    _validate_public_status(record.status)
    return HistorySession(
        session_id=record.id,
        title=history_title(record.first_user_message),
        scenario_id=record.scenario_id,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        expires_at=record.expires_at,
    )


def _detail_session(
    session: SessionRecord,
    first_turn: TurnRecord,
) -> HistorySession:
    _validate_public_status(session.status)
    return HistorySession(
        session_id=session.id,
        title=history_title(first_turn.user_message),
        scenario_id=session.scenario_id,
        status=session.status,
        created_at=session.created_at,
        updated_at=session.updated_at,
        expires_at=session.expires_at,
    )


def _history_turn(turn: TurnRecord) -> HistoryTurn:
    try:
        response = ConsultResponse.model_validate(turn.response)
    except ValidationError as exc:
        raise DataIntegrityError(
            "session_response_invalid",
            "历史咨询数据未通过完整性检查",
        ) from exc
    return HistoryTurn(
        turn_id=turn.id,
        user_message=turn.user_message,
        response=response,
        created_at=turn.created_at,
    )


def _validate_public_status(status: str) -> None:
    if status not in _PUBLIC_STATUSES:
        raise _history_integrity_error()


def _history_integrity_error() -> DataIntegrityError:
    return DataIntegrityError(
        "session_data_invalid",
        "历史咨询数据未通过完整性检查",
    )
