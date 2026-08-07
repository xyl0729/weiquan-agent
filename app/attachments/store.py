from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.attachments.errors import (
    AttachmentErrorCode,
    AttachmentInputError,
    AttachmentNotFoundError,
    AttachmentResourceLimitError,
    AttachmentStateConflictError,
)
from app.attachments.models import (
    AttachmentMediaType,
    ExtractionBlock,
    ExtractionResult,
    normalize_confirmed_text,
)
from app.db.models import AttachmentRecord, attachment_record_from_row
from app.db.session import SessionStore


AttachmentBinder = Callable[[sqlite3.Connection, str, str], None]

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
        "attachment_service_unavailable",
    }
)


class AttachmentStore:
    def __init__(
        self,
        sessions: SessionStore,
        *,
        draft_ttl_seconds: int = 3600,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if draft_ttl_seconds <= 0:
            raise ValueError("draft_ttl_seconds 必须大于 0")
        self.sessions = sessions
        self.draft_ttl = timedelta(seconds=draft_ttl_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    def create_processing(
        self,
        *,
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
            session_id=None,
            turn_id=None,
            turn_position=None,
            status="processing",
            original_name=original_name,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha256.lower(),
            page_count=None,
            extraction_method=None,
            extracted_blocks=(),
            confirmed_text=None,
            warnings=(),
            error_code=None,
            reservation_id=None,
            reserved_at=None,
            created_at=current,
            updated_at=current,
            expires_at=current + self.draft_ttl,
        )
        with self.sessions.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO attachments (
                    id, session_id, turn_id, turn_position, status,
                    original_name, media_type, size_bytes, sha256,
                    page_count, extraction_method, extracted_blocks_json,
                    confirmed_text, warnings_json, error_code,
                    reservation_id, reserved_at, created_at, updated_at,
                    expires_at
                ) VALUES (
                    ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, NULL, NULL,
                    '[]', NULL, '[]', NULL, NULL, NULL, ?, ?, ?
                )
                """,
                (
                    record.id,
                    record.status,
                    record.original_name,
                    record.media_type,
                    record.size_bytes,
                    record.sha256,
                    _iso(record.created_at),
                    _iso(record.updated_at),
                    _iso(_required_datetime(record.expires_at)),
                ),
            )
        return record

    def save_extraction(
        self,
        attachment_id: str,
        result: ExtractionResult,
        *,
        now: datetime | None = None,
    ) -> AttachmentRecord:
        normalized_id = _uuid(attachment_id)
        current = self._utc(now)
        with self.sessions.transaction(immediate=True) as connection:
            record = self._require_record(connection, normalized_id)
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

            candidate = _transition(
                record,
                status="review_required",
                page_count=result.page_count,
                extraction_method=result.extraction_method,
                extracted_blocks=result.blocks,
                confirmed_text=None,
                warnings=result.warnings,
                error_code=None,
                updated_at=current,
            )
            connection.execute(
                """
                UPDATE attachments
                SET status = ?, page_count = ?, extraction_method = ?,
                    extracted_blocks_json = ?, warnings_json = ?,
                    error_code = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    candidate.status,
                    candidate.page_count,
                    candidate.extraction_method,
                    _blocks_json(candidate.extracted_blocks),
                    _json_array(candidate.warnings),
                    _iso(candidate.updated_at),
                    normalized_id,
                ),
            )
            return self._require_record(connection, normalized_id)

    def save_failure(
        self,
        attachment_id: str,
        code: AttachmentErrorCode,
        *,
        now: datetime | None = None,
    ) -> AttachmentRecord:
        if code not in _PROCESSING_FAILURE_CODES:
            raise ValueError("错误代码不能作为附件处理失败结果")
        normalized_id = _uuid(attachment_id)
        current = self._utc(now)
        with self.sessions.transaction(immediate=True) as connection:
            record = self._require_record(connection, normalized_id)
            self._require_unexpired(record, current)
            if record.status == "bound":
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            if record.status != "processing":
                raise AttachmentStateConflictError(
                    "attachment_not_reviewable"
                )

            candidate = _transition(
                record,
                status="failed",
                page_count=None,
                extraction_method=None,
                extracted_blocks=(),
                confirmed_text=None,
                warnings=(),
                error_code=code,
                updated_at=current,
            )
            connection.execute(
                """
                UPDATE attachments
                SET status = ?, page_count = NULL,
                    extraction_method = NULL,
                    extracted_blocks_json = '[]',
                    confirmed_text = NULL, warnings_json = '[]',
                    error_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    candidate.status,
                    candidate.error_code,
                    _iso(candidate.updated_at),
                    normalized_id,
                ),
            )
            return self._require_record(connection, normalized_id)

    def confirm(
        self,
        attachment_id: str,
        confirmed_text: str,
        *,
        now: datetime | None = None,
    ) -> AttachmentRecord:
        normalized_id = _uuid(attachment_id)
        current = self._utc(now)
        normalized_text = normalize_confirmed_text(confirmed_text)
        assert normalized_text is not None

        with self.sessions.transaction(immediate=True) as connection:
            record = self._require_record(connection, normalized_id)
            self._require_unexpired(record, current)
            if record.status == "bound" or record.reservation_id is not None:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            if record.status not in {"review_required", "confirmed"}:
                raise AttachmentStateConflictError(
                    "attachment_not_reviewable"
                )

            candidate = _transition(
                record,
                status="confirmed",
                confirmed_text=normalized_text,
                updated_at=current,
            )
            connection.execute(
                """
                UPDATE attachments
                SET status = 'confirmed', confirmed_text = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    candidate.confirmed_text,
                    _iso(candidate.updated_at),
                    normalized_id,
                ),
            )
            return self._require_record(connection, normalized_id)

    def get(
        self,
        attachment_id: str,
        *,
        now: datetime | None = None,
    ) -> AttachmentRecord:
        record = self.get_optional(attachment_id, now=now)
        if record is None:
            raise AttachmentNotFoundError()
        return record

    def get_optional(
        self,
        attachment_id: str,
        *,
        now: datetime | None = None,
    ) -> AttachmentRecord | None:
        normalized_id = _uuid(attachment_id)
        current = self._utc(now)
        with self.sessions.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attachments WHERE id = ?",
                (normalized_id,),
            ).fetchone()
        if row is None:
            return None
        record = attachment_record_from_row(row)
        if record.expires_at is not None and record.expires_at <= current:
            return None
        return record

    def reserve(
        self,
        attachment_ids: Sequence[str],
        *,
        reservation_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        normalized_ids = _attachment_ids(attachment_ids)
        normalized_reservation = _uuid(reservation_id)
        current = self._utc(now)

        with self.sessions.transaction(immediate=True) as connection:
            existing_reservation = connection.execute(
                """
                SELECT 1
                FROM attachments
                WHERE reservation_id = ?
                LIMIT 1
                """,
                (normalized_reservation,),
            ).fetchone()
            if existing_reservation is not None:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )

            records = self._records_by_ids(connection, normalized_ids)
            for record in records:
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
                cursor = connection.execute(
                    """
                    UPDATE attachments
                    SET reservation_id = ?, reserved_at = ?,
                        turn_position = ?, updated_at = ?
                    WHERE id = ?
                      AND status = 'confirmed'
                      AND reservation_id IS NULL
                      AND session_id IS NULL
                      AND turn_id IS NULL
                    """,
                    (
                        normalized_reservation,
                        _iso(current),
                        position,
                        _iso(current),
                        attachment_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AttachmentStateConflictError(
                        "attachment_already_bound"
                    )
        return normalized_reservation

    def release(
        self,
        reservation_id: str,
        *,
        now: datetime | None = None,
    ) -> int:
        normalized_reservation = _uuid(reservation_id)
        current = self._utc(now)
        with self.sessions.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE attachments
                SET reservation_id = NULL, reserved_at = NULL,
                    turn_position = NULL, updated_at = ?
                WHERE reservation_id = ?
                  AND status = 'confirmed'
                  AND session_id IS NULL
                  AND turn_id IS NULL
                """,
                (_iso(current), normalized_reservation),
            )
        return max(cursor.rowcount, 0)

    def bind_reserved(
        self,
        reservation_id: str,
        *,
        session_id: str,
        turn_id: str,
        expected_ids: Sequence[str],
        now: datetime | None = None,
    ) -> list[AttachmentRecord]:
        normalized_turn_id = _uuid(turn_id)
        binder = self.reservation_binder(
            reservation_id,
            expected_ids=expected_ids,
            now=now,
        )
        with self.sessions.transaction(immediate=True) as connection:
            binder(connection, session_id, normalized_turn_id)
            rows = connection.execute(
                """
                SELECT *
                FROM attachments
                WHERE turn_id = ?
                ORDER BY turn_position, id
                """,
                (normalized_turn_id,),
            ).fetchall()
            records = [
                attachment_record_from_row(row)
                for row in rows
            ]
        return records

    def reservation_binder(
        self,
        reservation_id: str,
        *,
        expected_ids: Sequence[str],
        now: datetime | None = None,
    ) -> AttachmentBinder:
        normalized_reservation = _uuid(reservation_id)
        normalized_ids = _attachment_ids(expected_ids)
        current = self._utc(now)

        def bind(
            connection: sqlite3.Connection,
            session_id: str,
            turn_id: str,
        ) -> None:
            normalized_session_id = _uuid(session_id)
            normalized_turn_id = _uuid(turn_id)
            existing_binding = connection.execute(
                """
                SELECT 1
                FROM attachments
                WHERE turn_id = ?
                LIMIT 1
                """,
                (normalized_turn_id,),
            ).fetchone()
            if existing_binding is not None:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            rows = connection.execute(
                """
                SELECT *
                FROM attachments
                WHERE reservation_id = ?
                ORDER BY turn_position, id
                """,
                (normalized_reservation,),
            ).fetchall()
            records = [
                attachment_record_from_row(row)
                for row in rows
            ]
            stored_ids = tuple(record.id for record in records)
            if stored_ids != normalized_ids:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            for position, record in enumerate(records):
                self._require_unexpired(record, current)
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
                      AND status = 'confirmed'
                      AND reservation_id = ?
                      AND turn_position = ?
                    """,
                    (
                        normalized_session_id,
                        normalized_turn_id,
                        _iso(current),
                        record.id,
                        normalized_reservation,
                        position,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AttachmentStateConflictError(
                        "attachment_already_bound"
                    )

        return bind

    def list_for_turn(self, turn_id: str) -> list[AttachmentRecord]:
        normalized_turn_id = _uuid(turn_id)
        with self.sessions.transaction() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM attachments
                WHERE turn_id = ? AND status = 'bound'
                ORDER BY turn_position, id
                """,
                (normalized_turn_id,),
            ).fetchall()
        return [attachment_record_from_row(row) for row in rows]

    def delete(self, attachment_id: str) -> None:
        normalized_id = _uuid(attachment_id)
        with self.sessions.transaction(immediate=True) as connection:
            record = self._require_record(connection, normalized_id)
            if record.status == "bound" or record.reservation_id is not None:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            connection.execute(
                "DELETE FROM attachments WHERE id = ?",
                (normalized_id,),
            )

    def purge_expired(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        current = self._utc(now)
        with self.sessions.transaction(immediate=True) as connection:
            cursor = connection.execute(
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
                (_iso(current), int(limit)),
            )
        return max(cursor.rowcount, 0)

    def fail_stale_processing(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        current = self._utc(now)
        with self.sessions.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE attachments
                SET status = 'failed',
                    page_count = NULL,
                    extraction_method = NULL,
                    extracted_blocks_json = '[]',
                    confirmed_text = NULL,
                    warnings_json = '[]',
                    error_code = 'attachment_service_unavailable',
                    updated_at = ?
                WHERE id IN (
                    SELECT id
                    FROM attachments
                    WHERE status = 'processing'
                    ORDER BY created_at, id
                    LIMIT ?
                )
                """,
                (_iso(current), int(limit)),
            )
        return max(cursor.rowcount, 0)

    @staticmethod
    def _require_record(
        connection: sqlite3.Connection,
        attachment_id: str,
    ) -> AttachmentRecord:
        row = connection.execute(
            "SELECT * FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
        if row is None:
            raise AttachmentNotFoundError()
        return attachment_record_from_row(row)

    def _records_by_ids(
        self,
        connection: sqlite3.Connection,
        attachment_ids: tuple[str, ...],
    ) -> list[AttachmentRecord]:
        placeholders = ", ".join("?" for _ in attachment_ids)
        rows = connection.execute(
            f"""
            SELECT *
            FROM attachments
            WHERE id IN ({placeholders})
            """,
            attachment_ids,
        ).fetchall()
        by_id = {
            record.id: record
            for record in (
                attachment_record_from_row(row)
                for row in rows
            )
        }
        if any(attachment_id not in by_id for attachment_id in attachment_ids):
            raise AttachmentNotFoundError()
        return [by_id[attachment_id] for attachment_id in attachment_ids]

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


def _transition(
    record: AttachmentRecord,
    **changes: Any,
) -> AttachmentRecord:
    values = record.model_dump()
    values.update(changes)
    return AttachmentRecord.model_validate(values)


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


def _uuid(value: object | None) -> str:
    if value is None:
        return str(uuid4())
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("ID 必须是有效 UUID") from exc


def _blocks_json(blocks: Sequence[ExtractionBlock]) -> str:
    return _json_array(
        [block.model_dump(mode="json") for block in blocks]
    )


def _json_array(values: Sequence[Any]) -> str:
    return json.dumps(
        list(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _required_datetime(value: datetime | None) -> datetime:
    if value is None:
        raise RuntimeError("附件草稿缺少有效期")
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
