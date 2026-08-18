from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.models import UsageInfo
from app.attachments.errors import AttachmentErrorCode
from app.attachments.models import (
    AttachmentMediaType,
    AttachmentReviewPublic,
    AttachmentStatus,
    ExtractionBlock,
    ExtractionMethod,
    normalize_confirmed_text,
)


SessionStatus = Literal[
    "collecting",
    "need_more_facts",
    "ready",
    "escalate",
    "error",
]
AuditStatus = Literal["started", "ok", "error", "degraded"]


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    owner_id: str = Field(min_length=36, max_length=36)
    scenario_id: str | None = Field(default=None, max_length=100)
    facts: dict[str, Any] = Field(default_factory=dict)
    followup_round: int = Field(default=0, ge=0, le=2)
    status: SessionStatus = "collecting"
    jurisdiction: str | None = Field(default=None, max_length=100)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class SessionListRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    owner_id: str = Field(min_length=36, max_length=36)
    scenario_id: str | None = Field(default=None, max_length=100)
    status: SessionStatus
    first_user_message: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    owner_id: str = Field(min_length=36, max_length=36)
    session_id: str = Field(min_length=36, max_length=36)
    user_message: str = Field(min_length=1)
    facts: dict[str, Any] = Field(default_factory=dict)
    rule_matches: list[dict[str, Any]] = Field(default_factory=list)
    response: dict[str, Any] = Field(default_factory=dict)
    provider_name: str | None = Field(default=None, max_length=50)
    provider_model: str | None = Field(default=None, max_length=200)
    provider_request_id: str | None = Field(default=None, max_length=200)
    usage: UsageInfo = Field(default_factory=UsageInfo)
    created_at: datetime


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    owner_id: str = Field(min_length=36, max_length=36)
    audit_id: str = Field(min_length=36, max_length=36)
    session_id: str = Field(min_length=36, max_length=36)
    turn_id: str | None = Field(default=None, min_length=36, max_length=36)
    stage: str = Field(min_length=1, max_length=100)
    status: AuditStatus
    duration_ms: int = Field(default=0, ge=0)
    playbook_id: str | None = Field(default=None, max_length=100)
    playbook_version: str | None = Field(default=None, max_length=50)
    citations: list[str] = Field(default_factory=list)
    error_category: str | None = Field(default=None, max_length=100)
    created_at: datetime


class UsageDailyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: date
    client_hash: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=50)
    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    priced_request_count: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    updated_at: datetime


class RateLimitDailyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: date
    client_hash: str = Field(min_length=1, max_length=128)
    request_count: int = Field(ge=0)
    updated_at: datetime


class AttachmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    owner_id: str = Field(min_length=36, max_length=36)
    session_id: str | None = Field(default=None, min_length=36, max_length=36)
    turn_id: str | None = Field(default=None, min_length=36, max_length=36)
    turn_position: int | None = Field(default=None, ge=0, le=2)
    status: AttachmentStatus
    original_name: str
    media_type: AttachmentMediaType
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int | None = Field(default=None, ge=1)
    extraction_method: ExtractionMethod | None = None
    extracted_blocks: tuple[ExtractionBlock, ...] = Field(
        default_factory=tuple
    )
    confirmed_text: str | None = None
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    error_code: AttachmentErrorCode | None = None
    reservation_id: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
    )
    reserved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def record_is_consistent(self) -> "AttachmentRecord":
        public_values = {
            "id": self.id,
            "status": self.status,
            "original_name": self.original_name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "page_count": self.page_count,
            "extraction_method": self.extraction_method,
            "blocks": self.extracted_blocks,
            "warnings": self.warnings,
            "confirmed_text": self.confirmed_text,
            "error_code": self.error_code,
        }
        if (
            self.status in {"review_required", "confirmed", "bound"}
            and not self.extracted_blocks
        ):
            # PostgreSQL deliberately keeps unconfirmed OCR blocks out of
            # the database. Validate common public fields separately, then
            # enforce the durable extraction metadata below.
            AttachmentReviewPublic(
                **{
                    **public_values,
                    "status": "processing",
                    "extraction_method": None,
                    "confirmed_text": None,
                    "error_code": None,
                }
            )
            if self.page_count is None or self.extraction_method is None:
                raise ValueError("已提取附件必须包含提取元数据")
            if self.status == "review_required":
                if (
                    self.confirmed_text is not None
                    or self.error_code is not None
                ):
                    raise ValueError("待核对附件不能包含确认文字或错误")
            else:
                normalize_confirmed_text(self.confirmed_text)
                if self.error_code is not None:
                    raise ValueError("已确认附件不能包含错误")
        else:
            AttachmentReviewPublic(**public_values)

        if self.status == "bound":
            if (
                self.session_id is None
                or self.turn_id is None
                or self.turn_position is None
                or self.expires_at is not None
                or self.reservation_id is not None
                or self.reserved_at is not None
            ):
                raise ValueError("已绑定附件的关系字段无效")
            return self

        if self.session_id is not None or self.turn_id is not None:
            raise ValueError("未绑定附件不能关联会话或轮次")
        if self.expires_at is None:
            raise ValueError("未绑定附件必须具有草稿有效期")

        reservation_fields = (
            self.reservation_id,
            self.reserved_at,
            self.turn_position,
        )
        has_reservation = all(value is not None for value in reservation_fields)
        has_partial_reservation = any(
            value is not None for value in reservation_fields
        )
        if has_partial_reservation and not has_reservation:
            raise ValueError("附件预留字段必须同时存在")
        if has_reservation and self.status != "confirmed":
            raise ValueError("只有已确认附件可以被预留")
        return self


class SessionHistoryTurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: TurnRecord
    attachments: tuple[AttachmentRecord, ...] = Field(
        default_factory=tuple
    )


class SessionHistoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session: SessionRecord
    turns: tuple[SessionHistoryTurnRecord, ...] = Field(
        default_factory=tuple
    )


def attachment_record_from_row(row: sqlite3.Row) -> AttachmentRecord:
    blocks_data = _load_json_array(row["extracted_blocks_json"])
    warnings_data = _load_json_array(row["warnings_json"])
    return AttachmentRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        turn_position=row["turn_position"],
        status=row["status"],
        original_name=row["original_name"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        page_count=row["page_count"],
        extraction_method=row["extraction_method"],
        extracted_blocks=tuple(
            ExtractionBlock.model_validate(item)
            for item in blocks_data
        ),
        confirmed_text=row["confirmed_text"],
        warnings=tuple(warnings_data),
        error_code=row["error_code"],
        reservation_id=row["reservation_id"],
        reserved_at=row["reserved_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def _load_json_array(value: str) -> list[Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError("附件 JSON 数据必须是数组")
    return decoded
