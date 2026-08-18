from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.attachments.errors import (
    AttachmentResourceLimitError,
    AttachmentStateConflictError,
)
from app.attachments.models import AttachmentEvidenceContext
from app.db.contracts import AttachmentRepository


class EvidenceContextBuilder:
    def __init__(
        self,
        store: AttachmentRepository,
        *,
        max_attachments: int = 3,
        max_context_chars: int = 12_000,
    ) -> None:
        if not 1 <= max_attachments <= 3:
            raise ValueError("每轮附件上限必须介于 1 和 3 之间")
        if max_context_chars <= 0:
            raise ValueError("附件上下文字符上限必须大于 0")
        self.store = store
        self.max_attachments = max_attachments
        self.max_context_chars = max_context_chars

    def build(
        self,
        attachment_ids: Sequence[str],
        *,
        owner_id: str,
        reservation_id: str,
    ) -> tuple[AttachmentEvidenceContext, ...]:
        normalized_ids = _attachment_ids(attachment_ids)
        if not normalized_ids:
            return ()
        if len(normalized_ids) > self.max_attachments:
            raise AttachmentResourceLimitError(
                "attachment_count_exceeded"
            )
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("附件 ID 不得重复")

        normalized_reservation = _uuid(
            reservation_id,
            label="预留 ID",
        )
        evidence: list[AttachmentEvidenceContext] = []
        total_chars = 0
        for attachment_id in normalized_ids:
            record = self.store.get(
                attachment_id,
                owner_id=owner_id,
            )
            if record.status == "bound":
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )
            if (
                record.status != "confirmed"
                or record.confirmed_text is None
                or record.page_count is None
            ):
                raise AttachmentStateConflictError(
                    "attachment_not_confirmed"
                )
            if record.reservation_id != normalized_reservation:
                raise AttachmentStateConflictError(
                    "attachment_already_bound"
                )

            total_chars += len(record.confirmed_text)
            if total_chars > self.max_context_chars:
                raise AttachmentResourceLimitError(
                    "attachment_context_too_long"
                )
            evidence.append(
                AttachmentEvidenceContext(
                    id=record.id,
                    original_name=record.original_name,
                    media_type=record.media_type,
                    page_count=record.page_count,
                    confirmed_text=record.confirmed_text,
                )
            )
        return tuple(evidence)


def _attachment_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("附件 ID 必须是序列")
    return tuple(_uuid(value, label="附件 ID") for value in values)


def _uuid(value: object, *, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{label} 必须是有效 UUID") from exc
