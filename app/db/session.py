from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.agent.errors import (
    CaseNoProgressError,
    ConsultationConflictError,
    SessionNotFoundError,
)
from app.agent.models import UsageInfo
from app.attachments.errors import AttachmentStateConflictError
from app.db.contracts import (
    LOCAL_DEVELOPMENT_OWNER_ID,
    AttachmentBindingCommand,
    ConsultationCommitCommand,
)
from app.db.models import (
    AttachmentRecord,
    AuditRecord,
    RateLimitDailyRecord,
    SessionHistoryRecord,
    SessionHistoryTurnRecord,
    SessionListRecord,
    SessionRecord,
    TurnRecord,
    UsageDailyRecord,
    attachment_record_from_row,
)


SCHEMA_VERSION = 3
_UNSET = object()
_PURGE_LIMIT = 100
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

BASE_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    scenario_id TEXT,
    facts_json TEXT NOT NULL,
    followup_round INTEGER NOT NULL DEFAULT 0
        CHECK (followup_round BETWEEN 0 AND 2),
    status TEXT NOT NULL,
    jurisdiction TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    rule_matches_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    provider_name TEXT,
    provider_model TEXT,
    provider_request_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    estimated_cost_usd REAL CHECK (
        estimated_cost_usd IS NULL OR estimated_cost_usd >= 0
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_turns_session_created
ON turns(session_id, created_at);

CREATE TABLE IF NOT EXISTS audit_records (
    id TEXT PRIMARY KEY,
    audit_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    playbook_id TEXT,
    playbook_version TEXT,
    citations_json TEXT NOT NULL,
    error_category TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audit_group
ON audit_records(audit_id, created_at);

CREATE INDEX IF NOT EXISTS idx_audit_session
ON audit_records(session_id, created_at);

CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT NOT NULL,
    client_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    priced_request_count INTEGER NOT NULL DEFAULT 0
        CHECK (priced_request_count >= 0),
    estimated_cost_usd REAL NOT NULL DEFAULT 0
        CHECK (estimated_cost_usd >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, client_hash, provider)
);

CREATE TABLE IF NOT EXISTS rate_limit_daily (
    day TEXT NOT NULL,
    client_hash TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, client_hash)
);
"""

# Kept as a compatibility alias for callers that imported the v1 schema.
SCHEMA_SQL = BASE_SCHEMA_SQL

ATTACHMENT_SCHEMA_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_session_id
ON turns(session_id, id);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    turn_id TEXT,
    turn_position INTEGER,
    status TEXT NOT NULL CHECK (
        status IN (
            'processing',
            'review_required',
            'confirmed',
            'failed',
            'bound'
        )
    ),
    original_name TEXT NOT NULL CHECK (
        length(original_name) BETWEEN 1 AND 255
    ),
    media_type TEXT NOT NULL CHECK (
        media_type IN (
            'application/pdf',
            'image/png',
            'image/jpeg'
        )
    ),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    page_count INTEGER CHECK (
        page_count IS NULL OR page_count >= 1
    ),
    extraction_method TEXT CHECK (
        extraction_method IS NULL
        OR extraction_method IN ('direct_text', 'ocr', 'mixed')
    ),
    extracted_blocks_json TEXT NOT NULL DEFAULT '[]',
    confirmed_text TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    reservation_id TEXT,
    reserved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    CHECK (
        (
            status = 'bound'
            AND session_id IS NOT NULL
            AND turn_id IS NOT NULL
            AND turn_position IS NOT NULL
            AND reservation_id IS NULL
            AND reserved_at IS NULL
            AND expires_at IS NULL
        )
        OR
        (
            status <> 'bound'
            AND session_id IS NULL
            AND turn_id IS NULL
            AND expires_at IS NOT NULL
            AND (
                (
                    reservation_id IS NULL
                    AND reserved_at IS NULL
                    AND turn_position IS NULL
                )
                OR
                (
                    status = 'confirmed'
                    AND reservation_id IS NOT NULL
                    AND reserved_at IS NOT NULL
                    AND turn_position IS NOT NULL
                )
            )
        )
    ),
    CHECK (
        (
            status IN ('review_required', 'confirmed', 'bound')
            AND page_count IS NOT NULL
            AND extraction_method IS NOT NULL
            AND extracted_blocks_json <> '[]'
        )
        OR
        (
            status IN ('processing', 'failed')
            AND page_count IS NULL
            AND extraction_method IS NULL
            AND extracted_blocks_json = '[]'
        )
    ),
    CHECK (
        (
            status IN ('confirmed', 'bound')
            AND confirmed_text IS NOT NULL
            AND length(trim(confirmed_text)) > 0
        )
        OR
        (
            status IN ('processing', 'review_required', 'failed')
            AND confirmed_text IS NULL
        )
    ),
    CHECK (
        (status = 'failed' AND error_code IS NOT NULL)
        OR
        (status <> 'failed' AND error_code IS NULL)
    ),
    CHECK (
        turn_position IS NULL
        OR turn_position BETWEEN 0 AND 2
    ),
    FOREIGN KEY (session_id)
        REFERENCES sessions(id)
        ON DELETE CASCADE,
    FOREIGN KEY (turn_id)
        REFERENCES turns(id)
        ON DELETE CASCADE,
    FOREIGN KEY (session_id, turn_id)
        REFERENCES turns(session_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attachments_expires_at
ON attachments(expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_reservation_position
ON attachments(reservation_id, turn_position)
WHERE reservation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_attachments_turn
ON attachments(turn_id, turn_position);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_turn_position
ON attachments(turn_id, turn_position)
WHERE turn_id IS NOT NULL;
"""

OWNER_SCHEMA_SQL = f"""
ALTER TABLE sessions
ADD COLUMN owner_id TEXT NOT NULL
DEFAULT '{LOCAL_DEVELOPMENT_OWNER_ID}';

ALTER TABLE turns
ADD COLUMN owner_id TEXT NOT NULL
DEFAULT '{LOCAL_DEVELOPMENT_OWNER_ID}';

ALTER TABLE attachments
ADD COLUMN owner_id TEXT NOT NULL
DEFAULT '{LOCAL_DEVELOPMENT_OWNER_ID}';

ALTER TABLE audit_records
ADD COLUMN owner_id TEXT NOT NULL
DEFAULT '{LOCAL_DEVELOPMENT_OWNER_ID}';

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_owner_id
ON sessions(owner_id, id);

CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated
ON sessions(owner_id, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_owner_id
ON turns(owner_id, id);

CREATE INDEX IF NOT EXISTS idx_turns_owner_session_created
ON turns(owner_id, session_id, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_owner_id
ON attachments(owner_id, id);

CREATE INDEX IF NOT EXISTS idx_attachments_owner_expires
ON attachments(owner_id, expires_at);

CREATE INDEX IF NOT EXISTS idx_attachments_owner_turn
ON attachments(owner_id, turn_id, turn_position);

CREATE INDEX IF NOT EXISTS idx_audit_owner_session
ON audit_records(owner_id, session_id, created_at);
"""


class SessionStore:
    def __init__(
        self,
        path: Path,
        *,
        ttl_hours: int = 72,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_hours <= 0:
            raise ValueError("ttl_hours 必须大于 0")
        self.path = path.resolve()
        self.ttl = timedelta(hours=ttl_hours)
        self._now = now or (lambda: datetime.now(UTC))

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if version > SCHEMA_VERSION:
                raise RuntimeError("数据库 schema 版本高于当前程序")
            if version == SCHEMA_VERSION:
                return
            if version == 0 and self._has_user_tables(connection):
                raise RuntimeError("未识别的数据库 schema 版本")
            if version not in {0, 1, 2}:
                raise RuntimeError("不支持的数据库 schema 版本")

            schema = BASE_SCHEMA_SQL if version == 0 else ""
            attachment_schema = (
                ATTACHMENT_SCHEMA_SQL if version < 2 else ""
            )
            owner_schema = OWNER_SCHEMA_SQL if version < 3 else ""
            script = "\n".join(
                (
                    "BEGIN IMMEDIATE;",
                    schema,
                    attachment_schema,
                    owner_schema,
                    f"PRAGMA user_version = {SCHEMA_VERSION};",
                )
            )
            connection.executescript(script)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_session(
        self,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        scenario_id: str | None = None,
        facts: Mapping[str, Any] | None = None,
        jurisdiction: str | None = None,
        now: datetime | None = None,
        session_id: str | None = None,
    ) -> SessionRecord:
        created_at = self._utc(now)
        normalized_owner_id = _uuid(owner_id)
        normalized_id = _uuid(session_id)
        safe_facts = dict(facts or {})
        facts_json = _json(safe_facts)
        expires_at = created_at + self.ttl
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, owner_id, scenario_id, facts_json,
                    followup_round, status, jurisdiction, created_at,
                    updated_at, expires_at
                ) VALUES (?, ?, ?, ?, 0, 'collecting', ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    normalized_owner_id,
                    _optional_text(scenario_id, 100),
                    facts_json,
                    _optional_text(jurisdiction, 100),
                    _iso(created_at),
                    _iso(created_at),
                    _iso(expires_at),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_id, normalized_owner_id),
            ).fetchone()
        return _session_from_row(_required_row(row))

    def get_session(
        self,
        session_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        now: datetime | None = None,
    ) -> SessionRecord | None:
        normalized_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self._connect() as connection:
            self._purge(connection, current)
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_id, normalized_owner_id),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def require_session(
        self,
        session_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
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
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        now: datetime | None = None,
    ) -> list[SessionListRecord]:
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self._connect() as connection:
            self._purge(connection, current)
            rows = connection.execute(
                """
                SELECT
                    sessions.id,
                    sessions.owner_id,
                    sessions.scenario_id,
                    sessions.status,
                    sessions.created_at,
                    sessions.updated_at,
                    sessions.expires_at,
                    (
                        SELECT turns.user_message
                        FROM turns
                        WHERE turns.session_id = sessions.id
                          AND turns.owner_id = sessions.owner_id
                        ORDER BY turns.created_at, turns.id
                        LIMIT 1
                    ) AS first_user_message
                FROM sessions
                WHERE sessions.owner_id = ?
                  AND EXISTS (
                    SELECT 1
                    FROM turns
                    WHERE turns.session_id = sessions.id
                      AND turns.owner_id = sessions.owner_id
                )
                ORDER BY sessions.updated_at DESC, sessions.id
                """,
                (normalized_owner_id,),
            ).fetchall()
        return [_session_list_from_row(row) for row in rows]

    def get_session_history(
        self,
        session_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        now: datetime | None = None,
    ) -> SessionHistoryRecord | None:
        normalized_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self._connect() as connection:
            self._purge(connection, current)
            session_row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_id, normalized_owner_id),
            ).fetchone()
            if session_row is None:
                return None
            turn_rows = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id = ? AND owner_id = ?
                ORDER BY created_at, id
                """,
                (normalized_id, normalized_owner_id),
            ).fetchall()
            attachment_rows = connection.execute(
                """
                SELECT attachments.*
                FROM attachments
                LEFT JOIN turns AS related_turn
                  ON related_turn.id = attachments.turn_id
                 AND related_turn.owner_id = attachments.owner_id
                WHERE attachments.owner_id = ?
                  AND (
                    attachments.session_id = ?
                    OR related_turn.session_id = ?
                  )
                ORDER BY
                    attachments.turn_id,
                    attachments.turn_position,
                    attachments.id
                """,
                (
                    normalized_owner_id,
                    normalized_id,
                    normalized_id,
                ),
            ).fetchall()

            session = _session_from_row(session_row)
            turns = tuple(_turn_from_row(row) for row in turn_rows)
            turn_ids = {turn.id for turn in turns}
            attachments_by_turn = {
                turn.id: []
                for turn in turns
            }
            for row in attachment_rows:
                attachment = attachment_record_from_row(row)
                if (
                    attachment.status != "bound"
                    or attachment.session_id != session.id
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

    def delete_session(
        self,
        session_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        now: datetime | None = None,
    ) -> bool:
        normalized_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self._connect() as connection:
            self._purge(connection, current)
            cursor = connection.execute(
                """
                DELETE FROM sessions
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_id, normalized_owner_id),
            )
        return cursor.rowcount > 0

    def update_session(
        self,
        session_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        scenario_id: str | None | object = _UNSET,
        facts: Mapping[str, Any] | object = _UNSET,
        followup_round: int | object = _UNSET,
        status: str | object = _UNSET,
        jurisdiction: str | None | object = _UNSET,
        now: datetime | None = None,
    ) -> SessionRecord:
        normalized_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        current = self._utc(now)
        with self._connect() as connection:
            self._purge(connection, current)
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_id, normalized_owner_id),
            ).fetchone()
            if row is None:
                raise SessionNotFoundError()

            values = dict(row)
            if scenario_id is not _UNSET:
                values["scenario_id"] = _optional_text(
                    scenario_id,
                    100,
                )
            if facts is not _UNSET:
                if not isinstance(facts, Mapping):
                    raise TypeError("facts 必须是对象")
                values["facts_json"] = _json(dict(facts))
            if followup_round is not _UNSET:
                normalized_round = int(followup_round)
                if not 0 <= normalized_round <= 2:
                    raise ValueError("followup_round 必须在 0 到 2 之间")
                values["followup_round"] = normalized_round
            if status is not _UNSET:
                values["status"] = _session_status(status)
            if jurisdiction is not _UNSET:
                values["jurisdiction"] = _optional_text(
                    jurisdiction,
                    100,
                )

            expires_at = current + self.ttl
            connection.execute(
                """
                UPDATE sessions
                SET scenario_id = ?, facts_json = ?, followup_round = ?,
                    status = ?, jurisdiction = ?, updated_at = ?,
                    expires_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (
                    values["scenario_id"],
                    values["facts_json"],
                    values["followup_round"],
                    values["status"],
                    values["jurisdiction"],
                    _iso(current),
                    _iso(expires_at),
                    normalized_id,
                    normalized_owner_id,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM sessions
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_id, normalized_owner_id),
            ).fetchone()
        return _session_from_row(_required_row(updated))

    def add_turn(
        self,
        session_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        user_message: str,
        facts: Mapping[str, Any],
        rule_matches: Sequence[Mapping[str, Any]],
        response: Mapping[str, Any],
        provider_name: str | None = None,
        provider_model: str | None = None,
        provider_request_id: str | None = None,
        usage: UsageInfo | None = None,
        now: datetime | None = None,
        turn_id: str | None = None,
    ) -> TurnRecord:
        normalized_session_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        normalized_turn_id = _uuid(turn_id)
        current = self._utc(now)
        usage = usage or UsageInfo()
        safe_message = _redact_text(user_message.strip())
        if not safe_message:
            raise ValueError("user_message 不能为空")

        facts_json = _json(dict(facts))
        matches_json = _json([dict(item) for item in rule_matches])
        response_json = _json(dict(response))
        with self._connect() as connection:
            self._require_active(
                connection,
                normalized_session_id,
                normalized_owner_id,
                current,
            )
            connection.execute(
                """
                INSERT INTO turns (
                    id, owner_id, session_id, user_message, facts_json,
                    rule_matches_json, response_json, provider_name,
                    provider_model, provider_request_id, input_tokens,
                    output_tokens, total_tokens, estimated_cost_usd,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_turn_id,
                    normalized_owner_id,
                    normalized_session_id,
                    safe_message,
                    facts_json,
                    matches_json,
                    response_json,
                    _optional_text(provider_name, 50),
                    _optional_text(provider_model, 200),
                    _optional_text(provider_request_id, 200),
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.total_tokens,
                    usage.estimated_cost_usd,
                    _iso(current),
                ),
            )
            self._touch(
                connection,
                normalized_session_id,
                normalized_owner_id,
                current,
            )
            row = connection.execute(
                """
                SELECT * FROM turns
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_turn_id, normalized_owner_id),
            ).fetchone()
        return _turn_from_row(_required_row(row))

    def persist_session_turn(
        self,
        command: ConsultationCommitCommand,
    ) -> TurnRecord:
        normalized_owner_id = _uuid(command.owner_id)
        normalized_session_id = _uuid(command.session_id)
        normalized_turn_id = _uuid(command.turn.turn_id)
        current = self._utc(command.occurred_at)
        normalized_round = int(command.session.followup_round)
        if not 0 <= normalized_round <= 2:
            raise ValueError("followup_round 必须在 0 到 2 之间")
        normalized_status = _session_status(command.session.status)
        normalized_scenario = _optional_text(
            command.session.scenario_id,
            100,
        )
        normalized_jurisdiction = _optional_text(
            command.session.jurisdiction,
            100,
        )
        safe_facts = dict(command.session.facts)
        facts_json = _json(safe_facts)
        turn_facts_json = _json(dict(command.turn.facts))
        matches_json = _json(
            [dict(item) for item in command.turn.rule_matches]
        )
        response_json = _json(dict(command.turn.response))
        safe_message = _redact_text(command.turn.user_message.strip())
        if not safe_message:
            raise ValueError("user_message 不能为空")
        normalized_usage = command.turn.usage

        with self.transaction(immediate=True) as connection:
            self._require_active(
                connection,
                normalized_session_id,
                normalized_owner_id,
                current,
            )
            latest = connection.execute(
                """
                SELECT id, response_json
                FROM turns
                WHERE session_id = ? AND owner_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (normalized_session_id, normalized_owner_id),
            ).fetchone()
            _recheck_consultation_commit(command, latest)
            cursor = connection.execute(
                """
                UPDATE sessions
                SET scenario_id = ?, facts_json = ?, followup_round = ?,
                    status = ?, jurisdiction = ?, updated_at = ?,
                    expires_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (
                    normalized_scenario,
                    facts_json,
                    normalized_round,
                    normalized_status,
                    normalized_jurisdiction,
                    _iso(current),
                    _iso(current + self.ttl),
                    normalized_session_id,
                    normalized_owner_id,
                ),
            )
            if cursor.rowcount != 1:
                raise SessionNotFoundError()
            connection.execute(
                """
                INSERT INTO turns (
                    id, owner_id, session_id, user_message, facts_json,
                    rule_matches_json, response_json, provider_name,
                    provider_model, provider_request_id, input_tokens,
                    output_tokens, total_tokens, estimated_cost_usd,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_turn_id,
                    normalized_owner_id,
                    normalized_session_id,
                    safe_message,
                    turn_facts_json,
                    matches_json,
                    response_json,
                    _optional_text(command.turn.provider_name, 50),
                    _optional_text(command.turn.provider_model, 200),
                    _optional_text(
                        command.turn.provider_request_id,
                        200,
                    ),
                    normalized_usage.input_tokens,
                    normalized_usage.output_tokens,
                    normalized_usage.total_tokens,
                    normalized_usage.estimated_cost_usd,
                    _iso(current),
                ),
            )
            if command.attachment_binding is not None:
                self._bind_reserved_attachments(
                    connection,
                    command.attachment_binding,
                    owner_id=normalized_owner_id,
                    session_id=normalized_session_id,
                    turn_id=normalized_turn_id,
                    now=current,
                )
            row = connection.execute(
                """
                SELECT * FROM turns
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_turn_id, normalized_owner_id),
            ).fetchone()
        return _turn_from_row(_required_row(row))

    def bind_reserved_attachments(
        self,
        command: AttachmentBindingCommand,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        session_id: str,
        turn_id: str,
        now: datetime | None = None,
    ) -> list[AttachmentRecord]:
        normalized_owner_id = _uuid(owner_id)
        normalized_session_id = _uuid(session_id)
        normalized_turn_id = _uuid(turn_id)
        current = self._utc(now)
        with self.transaction(immediate=True) as connection:
            self._bind_reserved_attachments(
                connection,
                command,
                owner_id=normalized_owner_id,
                session_id=normalized_session_id,
                turn_id=normalized_turn_id,
                now=current,
            )
            rows = connection.execute(
                """
                SELECT *
                FROM attachments
                WHERE owner_id = ? AND turn_id = ?
                ORDER BY turn_position, id
                """,
                (normalized_owner_id, normalized_turn_id),
            ).fetchall()
        return [attachment_record_from_row(row) for row in rows]

    def list_turns(
        self,
        session_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> list[TurnRecord]:
        normalized_session_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id = ? AND owner_id = ?
                ORDER BY created_at, id
                """,
                (normalized_session_id, normalized_owner_id),
            ).fetchall()
        return [_turn_from_row(row) for row in rows]

    def add_audit_record(
        self,
        session_id: str,
        *,
        stage: str,
        status: str,
        audit_id: str | None = None,
        turn_id: str | None = None,
        duration_ms: int = 0,
        playbook_id: str | None = None,
        playbook_version: str | None = None,
        citations: Sequence[str] = (),
        error_category: str | None = None,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        now: datetime | None = None,
        record_id: str | None = None,
    ) -> AuditRecord:
        normalized_session_id = _uuid(session_id)
        normalized_owner_id = _uuid(owner_id)
        normalized_turn_id = _uuid(turn_id) if turn_id else None
        normalized_audit_id = _uuid(audit_id)
        normalized_record_id = _uuid(record_id)
        current = self._utc(now)
        normalized_stage = _required_text(stage, 100)
        normalized_status = _audit_status(status)
        if duration_ms < 0:
            raise ValueError("duration_ms 不能小于 0")
        citations_json = _json([_required_text(ref, 200) for ref in citations])

        with self._connect() as connection:
            self._require_active(
                connection,
                normalized_session_id,
                normalized_owner_id,
                current,
            )
            if normalized_turn_id is not None:
                turn = connection.execute(
                    """
                    SELECT 1 FROM turns
                    WHERE id = ? AND session_id = ? AND owner_id = ?
                    """,
                    (
                        normalized_turn_id,
                        normalized_session_id,
                        normalized_owner_id,
                    ),
                ).fetchone()
                if turn is None:
                    raise SessionNotFoundError()
            connection.execute(
                """
                INSERT INTO audit_records (
                    id, owner_id, audit_id, session_id, turn_id, stage,
                    status, duration_ms, playbook_id, playbook_version,
                    citations_json, error_category, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_record_id,
                    normalized_owner_id,
                    normalized_audit_id,
                    normalized_session_id,
                    normalized_turn_id,
                    normalized_stage,
                    normalized_status,
                    duration_ms,
                    _optional_text(playbook_id, 100),
                    _optional_text(playbook_version, 50),
                    citations_json,
                    _optional_text(error_category, 100),
                    _iso(current),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM audit_records
                WHERE id = ? AND owner_id = ?
                """,
                (normalized_record_id, normalized_owner_id),
            ).fetchone()
        return _audit_from_row(_required_row(row))

    def list_audit_records(
        self,
        *,
        audit_id: str | None = None,
        session_id: str | None = None,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> list[AuditRecord]:
        if (audit_id is None) == (session_id is None):
            raise ValueError("audit_id 和 session_id 必须且只能提供一个")
        field = "audit_id" if audit_id is not None else "session_id"
        value = _uuid(audit_id or session_id)
        normalized_owner_id = _uuid(owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM audit_records
                WHERE {field} = ? AND owner_id = ?
                ORDER BY created_at, id
                """,
                (value, normalized_owner_id),
            ).fetchall()
        return [_audit_from_row(row) for row in rows]

    def record_usage(
        self,
        *,
        client_hash: str,
        provider: str,
        usage: UsageInfo,
        now: datetime | None = None,
    ) -> UsageDailyRecord:
        current = self._utc(now)
        day = current.date().isoformat()
        normalized_client = _required_text(client_hash, 128)
        normalized_provider = _required_text(provider, 50)
        cost = usage.estimated_cost_usd
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_daily (
                    day, client_hash, provider, request_count,
                    input_tokens, output_tokens, total_tokens,
                    priced_request_count, estimated_cost_usd, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, client_hash, provider) DO UPDATE SET
                    request_count = request_count + 1,
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    total_tokens = total_tokens + excluded.total_tokens,
                    priced_request_count = (
                        priced_request_count + excluded.priced_request_count
                    ),
                    estimated_cost_usd = (
                        estimated_cost_usd + excluded.estimated_cost_usd
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    day,
                    normalized_client,
                    normalized_provider,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.total_tokens,
                    1 if cost is not None else 0,
                    cost or 0.0,
                    _iso(current),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM usage_daily
                WHERE day = ? AND client_hash = ? AND provider = ?
                """,
                (day, normalized_client, normalized_provider),
            ).fetchone()
        return _usage_from_row(_required_row(row))

    def get_usage(
        self,
        *,
        day: date,
        client_hash: str,
        provider: str,
    ) -> UsageDailyRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM usage_daily
                WHERE day = ? AND client_hash = ? AND provider = ?
                """,
                (
                    day.isoformat(),
                    _required_text(client_hash, 128),
                    _required_text(provider, 50),
                ),
            ).fetchone()
        return _usage_from_row(row) if row is not None else None

    def daily_estimated_cost(
        self,
        day: date,
        *,
        provider: str | None = None,
    ) -> float:
        query = (
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) AS total "
            "FROM usage_daily WHERE day = ?"
        )
        parameters: tuple[object, ...] = (day.isoformat(),)
        if provider is not None:
            query += " AND provider = ?"
            parameters += (_required_text(provider, 50),)
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return float(_required_row(row)["total"])

    def increment_rate_limit(
        self,
        *,
        client_hash: str,
        now: datetime | None = None,
    ) -> RateLimitDailyRecord:
        current = self._utc(now)
        day = current.date().isoformat()
        normalized_client = _required_text(client_hash, 128)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rate_limit_daily (
                    day, client_hash, request_count, updated_at
                ) VALUES (?, ?, 1, ?)
                ON CONFLICT(day, client_hash) DO UPDATE SET
                    request_count = request_count + 1,
                    updated_at = excluded.updated_at
                """,
                (day, normalized_client, _iso(current)),
            )
            row = connection.execute(
                """
                SELECT * FROM rate_limit_daily
                WHERE day = ? AND client_hash = ?
                """,
                (day, normalized_client),
            ).fetchone()
        return _rate_from_row(_required_row(row))

    def get_rate_limit(
        self,
        *,
        day: date,
        client_hash: str,
    ) -> RateLimitDailyRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM rate_limit_daily
                WHERE day = ? AND client_hash = ?
                """,
                (day.isoformat(), _required_text(client_hash, 128)),
            ).fetchone()
        return _rate_from_row(row) if row is not None else None

    def purge_expired(self, *, now: datetime | None = None) -> int:
        current = self._utc(now)
        with self._connect() as connection:
            return self._purge(connection, current)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self.transaction() as connection:
            yield connection

    @contextmanager
    def transaction(
        self,
        *,
        immediate: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            connection.execute(
                "BEGIN IMMEDIATE" if immediate else "BEGIN"
            )
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _has_user_tables(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
        return row is not None

    def _utc(self, value: datetime | None) -> datetime:
        current = value or self._now()
        if current.tzinfo is None:
            raise ValueError("时间必须包含时区")
        return current.astimezone(UTC)

    def _purge(
        self,
        connection: sqlite3.Connection,
        now: datetime,
        *,
        limit: int = _PURGE_LIMIT,
    ) -> int:
        if limit <= 0:
            return 0
        current = _iso(now)
        cursor = connection.execute(
            """
            DELETE FROM sessions
            WHERE id IN (
                SELECT id
                FROM sessions
                WHERE expires_at <= ?
                ORDER BY expires_at, id
                LIMIT ?
            )
            """,
            (current, limit),
        )
        connection.execute(
            """
            DELETE FROM attachments
            WHERE id IN (
                SELECT id
                FROM attachments
                WHERE session_id IS NULL
                  AND expires_at <= ?
                ORDER BY expires_at, id
                LIMIT ?
            )
            """,
            (current, limit),
        )
        return max(cursor.rowcount, 0)

    def _require_active(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        owner_id: str,
        now: datetime,
    ) -> None:
        self._purge(connection, now)
        row = connection.execute(
            """
            SELECT 1 FROM sessions
            WHERE id = ? AND owner_id = ?
            """,
            (session_id, owner_id),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError()

    def _bind_reserved_attachments(
        self,
        connection: sqlite3.Connection,
        command: AttachmentBindingCommand,
        *,
        owner_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> None:
        self._require_active(connection, session_id, owner_id, now)
        owned_turn = connection.execute(
            """
            SELECT 1 FROM turns
            WHERE id = ? AND owner_id = ?
            """,
            (turn_id, owner_id),
        ).fetchone()
        if owned_turn is None:
            raise SessionNotFoundError()

        reservation_id = _uuid(command.reservation_id)
        attachment_ids = tuple(
            _uuid(attachment_id)
            for attachment_id in command.attachment_ids
        )
        if not attachment_ids:
            raise ValueError("至少需要一个附件 ID")
        if len(attachment_ids) > 3:
            raise ValueError("每轮最多绑定三个附件")
        if len(attachment_ids) != len(set(attachment_ids)):
            raise ValueError("附件 ID 不得重复")

        existing_binding = connection.execute(
            """
            SELECT 1
            FROM attachments
            WHERE owner_id = ? AND turn_id = ?
            LIMIT 1
            """,
            (owner_id, turn_id),
        ).fetchone()
        if existing_binding is not None:
            raise AttachmentStateConflictError(
                "attachment_already_bound"
            )

        rows = connection.execute(
            """
            SELECT *
            FROM attachments
            WHERE owner_id = ? AND reservation_id = ?
            ORDER BY turn_position, id
            """,
            (owner_id, reservation_id),
        ).fetchall()
        records = [attachment_record_from_row(row) for row in rows]
        if tuple(record.id for record in records) != attachment_ids:
            raise AttachmentStateConflictError(
                "attachment_already_bound"
            )

        for position, record in enumerate(records):
            if record.expires_at is not None and record.expires_at <= now:
                raise AttachmentStateConflictError(
                    "attachment_not_confirmed"
                )
            if (
                record.status != "confirmed"
                or record.turn_position != position
            ):
                raise AttachmentStateConflictError(
                    "attachment_not_confirmed"
                )
            cursor = connection.execute(
                """
                UPDATE attachments
                SET status = 'bound', session_id = ?, turn_id = ?,
                    reservation_id = NULL, reserved_at = NULL,
                    expires_at = NULL, updated_at = ?
                WHERE id = ?
                  AND owner_id = ?
                  AND status = 'confirmed'
                  AND reservation_id = ?
                  AND turn_position = ?
                """,
                (
                    session_id,
                    turn_id,
                    _iso(now),
                    record.id,
                    owner_id,
                    reservation_id,
                    position,
                ),
            )
            if cursor.rowcount != 1:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )

    def _touch(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        owner_id: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            UPDATE sessions
            SET updated_at = ?, expires_at = ?
            WHERE id = ? AND owner_id = ?
            """,
            (
                _iso(now),
                _iso(now + self.ttl),
                session_id,
                owner_id,
            ),
        )


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        scenario_id=row["scenario_id"],
        facts=_load_json(row["facts_json"], dict),
        followup_round=row["followup_round"],
        status=row["status"],
        jurisdiction=row["jurisdiction"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def _session_list_from_row(row: sqlite3.Row) -> SessionListRecord:
    return SessionListRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        scenario_id=row["scenario_id"],
        status=row["status"],
        first_user_message=row["first_user_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def _turn_from_row(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        session_id=row["session_id"],
        user_message=row["user_message"],
        facts=_load_json(row["facts_json"], dict),
        rule_matches=_load_json(row["rule_matches_json"], list),
        response=_load_json(row["response_json"], dict),
        provider_name=row["provider_name"],
        provider_model=row["provider_model"],
        provider_request_id=row["provider_request_id"],
        usage=UsageInfo(
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            estimated_cost_usd=row["estimated_cost_usd"],
        ),
        created_at=row["created_at"],
    )


def _audit_from_row(row: sqlite3.Row) -> AuditRecord:
    return AuditRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        audit_id=row["audit_id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        stage=row["stage"],
        status=row["status"],
        duration_ms=row["duration_ms"],
        playbook_id=row["playbook_id"],
        playbook_version=row["playbook_version"],
        citations=_load_json(row["citations_json"], list),
        error_category=row["error_category"],
        created_at=row["created_at"],
    )


def _usage_from_row(row: sqlite3.Row) -> UsageDailyRecord:
    priced_count = int(row["priced_request_count"])
    return UsageDailyRecord(
        day=row["day"],
        client_hash=row["client_hash"],
        provider=row["provider"],
        request_count=row["request_count"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        total_tokens=row["total_tokens"],
        priced_request_count=priced_count,
        estimated_cost_usd=(
            float(row["estimated_cost_usd"])
            if priced_count
            else None
        ),
        updated_at=row["updated_at"],
    )


def _rate_from_row(row: sqlite3.Row) -> RateLimitDailyRecord:
    return RateLimitDailyRecord(
        day=row["day"],
        client_hash=row["client_hash"],
        request_count=row["request_count"],
        updated_at=row["updated_at"],
    )


def _json(value: object) -> str:
    _assert_safe_keys(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("数据必须可安全序列化为 JSON") from exc


def _load_json(value: str, expected: type[Any]) -> Any:
    decoded = json.loads(value)
    if not isinstance(decoded, expected):
        raise ValueError("数据库 JSON 类型不符合预期")
    return decoded


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


def _redact_text(value: str) -> str:
    return _SECRET_PATTERN.sub("[REDACTED]", value)


def _uuid(value: str | None) -> str:
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
    allowed = {
        "collecting",
        "need_more_facts",
        "ready",
        "escalate",
        "error",
    }
    if normalized not in allowed:
        raise ValueError("未知会话状态")
    return normalized


def _audit_status(value: object) -> str:
    normalized = _required_text(value, 30)
    if normalized not in {"started", "ok", "error", "degraded"}:
        raise ValueError("未知审计状态")
    return normalized


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _required_row(row: sqlite3.Row | None) -> sqlite3.Row:
    if row is None:
        raise RuntimeError("数据库写入后未返回记录")
    return row


def _recheck_consultation_commit(
    command: ConsultationCommitCommand,
    latest: sqlite3.Row | None,
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
    if latest_id != expected_id:
        if latest is not None:
            try:
                response = json.loads(latest["response_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                response = {}
            if isinstance(response, Mapping) and comparison_is_equivalent(
                command.comparison_units,
                response,
            ):
                raise CaseNoProgressError()
        raise ConsultationConflictError()

    if latest is None or not command.comparison_units:
        return
    try:
        response = json.loads(latest["response_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    if isinstance(response, Mapping) and comparison_is_equivalent(
        command.comparison_units,
        response,
    ):
        raise CaseNoProgressError()
