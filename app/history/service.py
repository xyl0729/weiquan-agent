from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.agent.errors import (
    DataIntegrityError,
    SessionNotFoundError,
    StorageUnavailableError,
)
from app.api.schemas import ConsultResponse
from app.attachments.projection import attachment_turn_public
from app.db.models import (
    SessionHistoryTurnRecord,
    SessionListRecord,
    SessionRecord,
    TurnRecord,
)
from app.db.session import SessionStore
from app.playbooks.registry import PlaybookRegistry


TITLE_LENGTH = 24
_TITLE_WHITESPACE = re.compile(r"\s+")
_PUBLIC_STATUSES = {"need_more_facts", "ready", "escalate"}
_TURN_KINDS = {
    "fact_collection",
    "initial_plan",
    "plan_update",
    "followup_answer",
    "new_case",
}
_LEGACY_REPEAT_REPLY = (
    "本轮未记录到新的方案变化，前一份方案仍然有效。"
)


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
    def __init__(
        self,
        store: SessionStore,
        registry: PlaybookRegistry,
    ) -> None:
        self.store = store
        self.registry = registry

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
        except (
            LookupError,
            TypeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise _history_integrity_error() from exc
        except (OSError, sqlite3.Error) as exc:
            raise StorageUnavailableError() from exc
        if stored is None:
            raise SessionNotFoundError()
        if not stored.turns:
            raise SessionNotFoundError()

        try:
            allowed_citations = _allowed_citation_refs(
                self.registry,
                stored.session.scenario_id,
            )
            public_turns = _project_history_turns(
                stored.turns,
                allowed_citations=allowed_citations,
            )
        except DataIntegrityError:
            raise
        except (LookupError, TypeError, ValidationError, ValueError) as exc:
            raise _history_integrity_error() from exc
        return SessionDetail(
            session=_detail_session(
                stored.session,
                stored.turns[0].turn,
            ),
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


def _history_turn(
    turn: TurnRecord,
    response_payload: Mapping[str, Any],
) -> HistoryTurn:
    try:
        response = ConsultResponse.model_validate(response_payload)
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


def _project_history_turns(
    turns: Sequence[SessionHistoryTurnRecord],
    *,
    allowed_citations: set[str],
) -> tuple[HistoryTurn, ...]:
    projected: list[HistoryTurn] = []
    previous_plan: dict[str, Any] | None = None

    for stored_turn in turns:
        turn = stored_turn.turn
        payload = deepcopy(turn.response)
        payload["attachments"] = [
            attachment_turn_public(attachment).model_dump(mode="json")
            for attachment in stored_turn.attachments
        ]
        signature = _plan_signature(payload)
        explicit_kind = payload.get("turn_kind")

        if explicit_kind is None:
            if signature is None:
                payload["turn_kind"] = "fact_collection"
                payload.setdefault("reply", None)
            elif previous_plan is None:
                payload["turn_kind"] = "initial_plan"
                payload.setdefault("reply", None)
            elif signature != previous_plan:
                payload["turn_kind"] = "plan_update"
                payload.setdefault("reply", None)
            else:
                payload.update(
                    {
                        "turn_kind": "followup_answer",
                        "verdict": None,
                        "plan": None,
                        "reply": {
                            "text": _LEGACY_REPEAT_REPLY,
                            "suggested_actions": [],
                            "citation_refs": [],
                            "new_case": None,
                        },
                        "citations": [],
                    }
                )
        elif (
            not isinstance(explicit_kind, str)
            or explicit_kind not in _TURN_KINDS
        ):
            # Keep the invalid value so public-schema validation fails closed.
            pass

        payload = _filter_citations(
            payload,
            allowed_citations=allowed_citations,
        )
        history_turn = _history_turn(turn, payload)
        projected.append(history_turn)

        current_signature = _plan_signature(payload)
        if current_signature is not None:
            previous_plan = current_signature

    return tuple(projected)


def _plan_signature(
    response: Mapping[str, Any],
) -> dict[str, Any] | None:
    plan = response.get("plan")
    verdict = response.get("verdict")
    if not isinstance(plan, Mapping) or not isinstance(verdict, Mapping):
        return None

    normalized_plan = {
        key: deepcopy(value)
        for key, value in plan.items()
        if key not in {"rendered_text", "evidence_request_text"}
    }
    return {
        "verdict": deepcopy(dict(verdict)),
        "plan": normalized_plan,
    }


def _filter_citations(
    response: Mapping[str, Any],
    *,
    allowed_citations: set[str],
) -> dict[str, Any]:
    payload = deepcopy(dict(response))
    citations = payload.get("citations")
    filtered_any = False
    if isinstance(citations, list):
        filtered = [
            item
            for item in citations
            if (
                isinstance(item, Mapping)
                and item.get("ref") in allowed_citations
            )
        ]
        filtered_any = len(filtered) != len(citations)
        payload["citations"] = filtered

    reply = payload.get("reply")
    if isinstance(reply, Mapping):
        projected_reply = dict(reply)
        citation_refs = projected_reply.get("citation_refs")
        if isinstance(citation_refs, list):
            projected_reply["citation_refs"] = [
                ref
                for ref in citation_refs
                if isinstance(ref, str) and ref in allowed_citations
            ]
        payload["reply"] = projected_reply

    plan = payload.get("plan")
    if filtered_any and isinstance(plan, Mapping):
        projected_plan = dict(plan)
        projected_plan["rendered_text"] = None
        payload["plan"] = projected_plan
    return payload


def _allowed_citation_refs(
    registry: PlaybookRegistry,
    scenario_id: str | None,
) -> set[str]:
    if scenario_id is None:
        return set()
    playbook = registry.get(scenario_id)
    return {basis.ref for basis in playbook.legal_basis}


def _validate_public_status(status: str) -> None:
    if status not in _PUBLIC_STATUSES:
        raise _history_integrity_error()


def _history_integrity_error() -> DataIntegrityError:
    return DataIntegrityError(
        "session_data_invalid",
        "历史咨询数据未通过完整性检查",
    )
