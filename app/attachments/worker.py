from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)

from app.attachments.errors import AttachmentError, AttachmentErrorCode
from app.attachments.extractors import LocalDocumentExtractor
from app.attachments.models import ExtractionResult


_MAX_JOB_BYTES = 32 * 1024
_MAX_RESULT_BYTES = 8 * 1024 * 1024
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


class WorkerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_file_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    max_pdf_pages: int = Field(gt=0, le=100)
    max_image_pixels: int = Field(gt=0, le=100_000_000)
    max_extracted_chars: int = Field(gt=0, le=1_000_000)
    low_confidence_threshold: float = Field(ge=0, le=1)


class WorkerJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: str = Field(min_length=1, max_length=4096)
    declared_media_type: str | None = Field(default=None, max_length=255)
    limits: WorkerLimits


class WorkerSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    result: ExtractionResult


class WorkerFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["error"] = "error"
    error_code: AttachmentErrorCode

    @field_validator("error_code")
    @classmethod
    def error_is_a_processing_failure(
        cls,
        value: AttachmentErrorCode,
    ) -> AttachmentErrorCode:
        if value not in _PROCESSING_FAILURE_CODES:
            raise ValueError("worker 错误代码不是处理失败")
        return value


WorkerEnvelope: TypeAlias = Annotated[
    WorkerSuccess | WorkerFailure,
    Field(discriminator="status"),
]
WORKER_ENVELOPE_ADAPTER = TypeAdapter(WorkerEnvelope)


def parse_worker_envelope(value: bytes | str) -> WorkerEnvelope:
    return WORKER_ENVELOPE_ADAPTER.validate_json(value)


def _extract(job: WorkerJob) -> WorkerEnvelope:
    source_path = Path(job.source_path)
    limits = job.limits
    extractor = LocalDocumentExtractor(
        max_file_bytes=limits.max_file_bytes,
        max_pdf_pages=limits.max_pdf_pages,
        max_image_pixels=limits.max_image_pixels,
        max_extracted_chars=limits.max_extracted_chars,
        low_confidence_threshold=limits.low_confidence_threshold,
    )
    try:
        result = extractor.extract(
            source_path,
            declared_media_type=job.declared_media_type,
        )
    except AttachmentError as exc:
        return WorkerFailure(error_code=exc.code)
    except Exception:
        return WorkerFailure(
            error_code="attachment_service_unavailable"
        )
    return WorkerSuccess(result=result)


def _read_job(path: Path) -> WorkerJob:
    size = path.stat().st_size
    if size <= 0 or size > _MAX_JOB_BYTES:
        raise ValueError("invalid worker job size")
    return WorkerJob.model_validate_json(path.read_bytes())


def _validated_paths(
    job_path: Path,
    result_path: Path,
    job: WorkerJob,
) -> Path:
    job_file = job_path.resolve(strict=True)
    result_file = result_path.resolve(strict=False)
    source_file = Path(job.source_path).resolve(strict=True)
    root = job_file.parent

    if result_file.parent != root or source_file.parent != root:
        raise ValueError("worker paths must share a controlled directory")
    if not job_file.name.endswith(".job.json"):
        raise ValueError("invalid worker job name")
    token = job_file.name.removesuffix(".job.json")
    if (
        len(token) != 32
        or any(char not in "0123456789abcdef" for char in token)
        or source_file.name != f"{token}.source"
        or result_file.name != f"{token}.result.json"
    ):
        raise ValueError("worker paths do not share a random token")
    return result_file


def _write_result(path: Path, envelope: WorkerEnvelope) -> None:
    payload = envelope.model_dump_json()
    encoded = payload.encode("utf-8")
    if not encoded or len(encoded) > _MAX_RESULT_BYTES:
        encoded = WorkerFailure(
            error_code="attachment_service_unavailable"
        ).model_dump_json().encode("utf-8")
    with path.open("xb") as output:
        output.write(encoded)
        output.flush()


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) != 2:
        return 2
    job_path = Path(args[0])
    result_path = Path(args[1])
    try:
        job = _read_job(job_path)
        safe_result_path = _validated_paths(job_path, result_path, job)
        envelope = _extract(job)
        _write_result(safe_result_path, envelope)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
