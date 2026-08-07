from __future__ import annotations

import re
import unicodedata
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.attachments.errors import AttachmentErrorCode


AttachmentStatus: TypeAlias = Literal[
    "processing",
    "review_required",
    "confirmed",
    "failed",
    "bound",
]
AttachmentMediaType: TypeAlias = Literal[
    "application/pdf",
    "image/png",
    "image/jpeg",
]
ExtractionMethod: TypeAlias = Literal[
    "direct_text",
    "ocr",
    "mixed",
]

_WARNING_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
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


def _validate_original_name(value: str) -> str:
    if not value.strip():
        raise ValueError("文件名不能为空")
    if len(value) > 255:
        raise ValueError("文件名不能超过 255 个字符")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError("文件名不能包含控制字符")
    return value


def _validate_confirmed_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError("确认文字不能为空")
    if len(normalized) > 200_000:
        raise ValueError("确认文字不能超过 200000 个字符")
    return normalized


def _validate_warnings(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError("附件警告代码不得重复")
    if any(_WARNING_CODE.fullmatch(value) is None for value in values):
        raise ValueError("附件警告代码无效")
    return values


class ExtractionBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(ge=1)
    block_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)

    @field_validator("text")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("提取文字不能为空")
        return normalized


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    media_type: AttachmentMediaType
    page_count: int = Field(ge=1)
    extraction_method: ExtractionMethod
    blocks: tuple[ExtractionBlock, ...] = Field(min_length=1)
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    width_px: int | None = Field(default=None, ge=1)
    height_px: int | None = Field(default=None, ge=1)

    @field_validator("warnings")
    @classmethod
    def warnings_are_valid(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_warnings(values)

    @model_validator(mode="after")
    def result_is_consistent(self) -> "ExtractionResult":
        positions = [
            (block.page_number, block.block_index)
            for block in self.blocks
        ]
        if len(positions) != len(set(positions)):
            raise ValueError("提取块位置不得重复")
        if any(block.page_number > self.page_count for block in self.blocks):
            raise ValueError("提取块页码不能超过总页数")

        dimensions = (self.width_px, self.height_px)
        if self.media_type == "application/pdf":
            if any(value is not None for value in dimensions):
                raise ValueError("PDF 提取结果不得包含单图尺寸")
        else:
            if self.page_count != 1:
                raise ValueError("图片提取结果只能有一页")
            if any(value is None for value in dimensions):
                raise ValueError("图片提取结果必须包含完整尺寸")
            if self.extraction_method != "ocr":
                raise ValueError("图片只能使用 OCR 提取")
        return self


class AttachmentReviewPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    status: AttachmentStatus
    original_name: str
    media_type: AttachmentMediaType | None = None
    size_bytes: int = Field(ge=0)
    page_count: int | None = Field(default=None, ge=1)
    extraction_method: ExtractionMethod | None = None
    blocks: tuple[ExtractionBlock, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    confirmed_text: str | None = None
    error_code: AttachmentErrorCode | None = None

    @field_validator("original_name")
    @classmethod
    def original_name_is_safe(cls, value: str) -> str:
        return _validate_original_name(value)

    @field_validator("confirmed_text")
    @classmethod
    def confirmed_text_is_valid(cls, value: str | None) -> str | None:
        return _validate_confirmed_text(value)

    @field_validator("warnings")
    @classmethod
    def warnings_are_valid(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_warnings(values)

    @model_validator(mode="after")
    def status_data_is_consistent(self) -> "AttachmentReviewPublic":
        has_extraction = (
            self.media_type is not None
            and self.page_count is not None
            and self.extraction_method is not None
            and bool(self.blocks)
        )
        if self.status == "processing":
            if (
                self.blocks
                or self.confirmed_text is not None
                or self.error_code is not None
                or self.extraction_method is not None
            ):
                raise ValueError("处理中附件不能包含结果、确认文字或错误")
        elif self.status == "review_required":
            if (
                not has_extraction
                or self.confirmed_text is not None
                or self.error_code is not None
            ):
                raise ValueError("待核对附件必须只包含可核对提取结果")
        elif self.status in {"confirmed", "bound"}:
            if (
                not has_extraction
                or self.confirmed_text is None
                or self.error_code is not None
            ):
                raise ValueError("已确认附件必须包含提取结果和确认文字")
        else:
            if (
                self.error_code not in _PROCESSING_FAILURE_CODES
                or self.blocks
                or self.confirmed_text is not None
            ):
                raise ValueError("失败附件必须只包含安全处理错误")
        return self


class AttachmentTurnPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    status: Literal["bound"] = "bound"
    original_name: str
    media_type: AttachmentMediaType
    size_bytes: int = Field(ge=0)
    page_count: int = Field(ge=1)
    extraction_method: ExtractionMethod
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    confirmed_text: str

    @field_validator("original_name")
    @classmethod
    def original_name_is_safe(cls, value: str) -> str:
        return _validate_original_name(value)

    @field_validator("confirmed_text")
    @classmethod
    def confirmed_text_is_valid(cls, value: str) -> str:
        normalized = _validate_confirmed_text(value)
        assert normalized is not None
        return normalized

    @field_validator("warnings")
    @classmethod
    def warnings_are_valid(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_warnings(values)


class AttachmentEvidenceContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    original_name: str
    media_type: AttachmentMediaType
    page_count: int = Field(ge=1)
    confirmed_text: str

    @field_validator("original_name")
    @classmethod
    def original_name_is_safe(cls, value: str) -> str:
        return _validate_original_name(value)

    @field_validator("confirmed_text")
    @classmethod
    def confirmed_text_is_valid(cls, value: str) -> str:
        normalized = _validate_confirmed_text(value)
        assert normalized is not None
        return normalized
