from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterable, Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from app.attachments.errors import (
    AttachmentError,
    AttachmentInputError,
    AttachmentResourceLimitError,
    AttachmentServiceUnavailableError,
    build_attachment_error,
)
from app.attachments.drafts import OcrDraftStore
from app.attachments.models import (
    AttachmentMediaType,
    ExtractionResult,
    validate_attachment_name,
)
from app.attachments.worker import (
    WorkerFailure,
    WorkerJob,
    WorkerLimits,
    parse_worker_envelope,
)
from app.config import PROJECT_ROOT, _is_test_staging_path
from app.db.contracts import (
    LOCAL_DEVELOPMENT_OWNER_ID,
    AttachmentRepository,
)
from app.db.models import AttachmentRecord
from app.execution.bounded import (
    BoundedExecutionBusyError,
    BoundedExecutionTimeoutError,
    BoundedExecutor,
)


_PDF_SIGNATURE = b"%PDF-"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_ALLOWED_MEDIA_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg"}
)
_MANAGED_NAME = re.compile(
    r"^[0-9a-f]{32}\.(?:source|job\.json|result\.json)$"
)
_MAX_MULTIPART_OVERHEAD = 64 * 1024
_MAX_WORKER_RESULT_BYTES = 8 * 1024 * 1024
_DEFAULT_CLEANUP_LIMIT = 100
_WORKER_ENVIRONMENT_NAMES = frozenset(
    {
        "COMSPEC",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
    }
)


class ExtractionWorker(Protocol):
    async def extract(
        self,
        source_path: Path,
        *,
        declared_media_type: str | None,
        limits: WorkerLimits,
        job_path: Path,
        result_path: Path,
    ) -> ExtractionResult:
        ...


class WorkerProcess(Protocol):
    returncode: int | None

    async def wait(self) -> int:
        ...

    def terminate(self) -> None:
        ...

    def kill(self) -> None:
        ...


ProcessFactory = Callable[..., Awaitable[WorkerProcess]]


class SubprocessExtractionWorker:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        termination_grace_seconds: float = 1,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("worker timeout 必须大于 0")
        if termination_grace_seconds <= 0:
            raise ValueError("worker 终止等待必须大于 0")
        self.timeout_seconds = float(timeout_seconds)
        self.termination_grace_seconds = float(
            termination_grace_seconds
        )
        self._process_factory = (
            process_factory or asyncio.create_subprocess_exec
        )

    async def extract(
        self,
        source_path: Path,
        *,
        declared_media_type: str | None,
        limits: WorkerLimits,
        job_path: Path,
        result_path: Path,
    ) -> ExtractionResult:
        _require_shared_temp_paths(
            source_path,
            job_path,
            result_path,
        )
        job = WorkerJob(
            source_path=str(source_path),
            declared_media_type=declared_media_type,
            limits=limits,
        )
        try:
            with job_path.open("x", encoding="utf-8", newline="\n") as file:
                file.write(job.model_dump_json())
            process = await self._start_process(job_path, result_path)
        except (OSError, ValueError):
            raise AttachmentServiceUnavailableError() from None

        try:
            return_code = await asyncio.wait_for(
                process.wait(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            await self._stop_process(process)
            raise AttachmentInputError(
                "attachment_extraction_timeout"
            ) from None
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_process(process))
            raise
        except Exception:
            await self._stop_process(process)
            raise AttachmentServiceUnavailableError() from None

        if return_code != 0:
            raise AttachmentServiceUnavailableError()
        envelope = _read_worker_result(result_path)
        if isinstance(envelope, WorkerFailure):
            raise build_attachment_error(envelope.error_code)
        return envelope.result

    async def _start_process(
        self,
        job_path: Path,
        result_path: Path,
    ) -> WorkerProcess:
        creation_flags = (
            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        process = await self._process_factory(
            sys.executable,
            "-E",
            "-s",
            "-m",
            "app.attachments.worker",
            str(job_path),
            str(result_path),
            cwd=str(PROJECT_ROOT),
            env=_worker_environment(job_path.parent),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        return cast(WorkerProcess, process)

    async def _stop_process(self, process: WorkerProcess) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self.termination_grace_seconds,
            )
            return
        except TimeoutError:
            pass
        except ProcessLookupError:
            return
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await process.wait()
        except ProcessLookupError:
            return


class AttachmentService:
    def __init__(
        self,
        store: AttachmentRepository,
        *,
        temp_dir: Path,
        max_file_bytes: int,
        max_pdf_pages: int,
        max_image_pixels: int,
        max_extracted_chars: int,
        low_confidence_threshold: float,
        extraction_timeout_seconds: float,
        worker: ExtractionWorker | object | None = None,
        draft_store: OcrDraftStore | None = None,
        executor: BoundedExecutor | None = None,
    ) -> None:
        self.store = store
        self.temp_dir = _validated_temp_directory(temp_dir)
        self.drafts = draft_store or OcrDraftStore(self.temp_dir)
        self.limits = WorkerLimits(
            max_file_bytes=max_file_bytes,
            max_pdf_pages=max_pdf_pages,
            max_image_pixels=max_image_pixels,
            max_extracted_chars=max_extracted_chars,
            low_confidence_threshold=low_confidence_threshold,
        )
        self.worker = cast(
            ExtractionWorker,
            worker
            or SubprocessExtractionWorker(
                timeout_seconds=extraction_timeout_seconds
            ),
        )
        self.extraction_timeout_seconds = float(
            extraction_timeout_seconds
        )
        self.executor = executor or BoundedExecutor(
            name="ocr",
            max_concurrency=1,
            max_waiting=2,
        )
        self._active_paths: set[Path] = set()

    async def upload(
        self,
        request: Any,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> AttachmentRecord:
        return await self.process_multipart(
            request.headers.get("content-type"),
            request.stream(),
            owner_id=owner_id,
        )

    async def process_multipart(
        self,
        content_type: str | None,
        chunks: AsyncIterable[bytes],
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> AttachmentRecord:
        normalized_owner_id = _uuid(owner_id)
        boundary = _multipart_boundary(content_type)
        self._ensure_temp_directory()
        source_path, job_path, result_path = self._new_paths()
        managed_paths = (source_path, job_path, result_path)
        self._active_paths.update(managed_paths)

        record: AttachmentRecord | None = None
        try:
            self.cleanup_orphans()
            upload = await _write_multipart_file(
                boundary,
                chunks,
                source_path,
                max_file_bytes=self.limits.max_file_bytes,
            )
            media_type = _detect_media_type(upload.signature)
            _validate_declared_media_type(
                media_type,
                upload.declared_media_type,
            )
            record = self.store.create_processing(
                owner_id=normalized_owner_id,
                original_name=upload.original_name,
                media_type=media_type,
                size_bytes=upload.size_bytes,
                sha256=upload.sha256,
            )
            try:
                extraction = await self.executor.run(
                    lambda: self.worker.extract(
                        source_path,
                        declared_media_type=upload.declared_media_type,
                        limits=self.limits,
                        job_path=job_path,
                        result_path=result_path,
                    ),
                    total_timeout_seconds=(
                        self.extraction_timeout_seconds
                    ),
                )
            except asyncio.CancelledError:
                self._save_failure_after_cancellation(
                    record.id,
                    owner_id=normalized_owner_id,
                )
                raise
            except BoundedExecutionBusyError:
                return self.store.save_failure(
                    record.id,
                    "attachment_service_busy",
                    owner_id=normalized_owner_id,
                )
            except BoundedExecutionTimeoutError:
                return self.store.save_failure(
                    record.id,
                    "attachment_extraction_timeout",
                    owner_id=normalized_owner_id,
                )
            except AttachmentError as exc:
                return self.store.save_failure(
                    record.id,
                    exc.code,
                    owner_id=normalized_owner_id,
                )
            except Exception:
                return self.store.save_failure(
                    record.id,
                    "attachment_service_unavailable",
                    owner_id=normalized_owner_id,
                )
            if extraction.media_type != media_type:
                return self.store.save_failure(
                    record.id,
                    "attachment_type_mismatch",
                    owner_id=normalized_owner_id,
                )
            try:
                self.drafts.save(
                    record.id,
                    extraction,
                    owner_id=normalized_owner_id,
                )
            except (OSError, ValueError):
                return self.store.save_failure(
                    record.id,
                    "attachment_service_unavailable",
                    owner_id=normalized_owner_id,
                )
            try:
                saved = self.store.save_extraction(
                    record.id,
                    extraction,
                    owner_id=normalized_owner_id,
                )
                return self._hydrate_review(
                    saved,
                    owner_id=normalized_owner_id,
                )
            except BaseException as exc:
                self._delete_draft_after_failure(
                    record.id,
                    owner_id=normalized_owner_id,
                )
                if isinstance(exc, AttachmentError) or not isinstance(
                    exc,
                    Exception,
                ):
                    raise
                raise AttachmentServiceUnavailableError() from exc
        finally:
            for path in managed_paths:
                _safe_unlink(path, self.temp_dir)
            self._active_paths.difference_update(managed_paths)

    def recover(
        self,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        limit: int = _DEFAULT_CLEANUP_LIMIT,
    ) -> tuple[int, int]:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        normalized_owner_id = _uuid(owner_id)
        self._ensure_temp_directory()
        recovered = 0
        try:
            recovered = self.store.fail_stale_processing(
                owner_id=normalized_owner_id,
                limit=limit,
            )
        finally:
            removed = self.cleanup_orphans(limit=limit)
            removed += self.drafts.cleanup_all(limit=limit)
        return recovered, removed

    def get(
        self,
        attachment_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> AttachmentRecord:
        normalized_owner_id = _uuid(owner_id)
        record = self.store.get(
            attachment_id,
            owner_id=normalized_owner_id,
        )
        return self._hydrate_review(record, owner_id=normalized_owner_id)

    def confirm(
        self,
        attachment_id: str,
        confirmed_text: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> AttachmentRecord:
        normalized_owner_id = _uuid(owner_id)
        current = self.store.get(
            attachment_id,
            owner_id=normalized_owner_id,
        )
        current = self._hydrate_review(
            current,
            owner_id=normalized_owner_id,
        )
        record = self.store.confirm(
            attachment_id,
            confirmed_text,
            owner_id=normalized_owner_id,
        )
        if not record.extracted_blocks:
            record = record.model_copy(
                update={"extracted_blocks": current.extracted_blocks}
            )
        try:
            self.drafts.delete(
                record.id,
                owner_id=normalized_owner_id,
            )
        except OSError:
            raise AttachmentServiceUnavailableError() from None
        return record

    def delete(
        self,
        attachment_id: str,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
    ) -> None:
        normalized_owner_id = _uuid(owner_id)
        self.store.delete(
            attachment_id,
            owner_id=normalized_owner_id,
        )
        try:
            self.drafts.delete(
                attachment_id,
                owner_id=normalized_owner_id,
            )
        except OSError:
            raise AttachmentServiceUnavailableError() from None

    def purge_expired(
        self,
        *,
        owner_id: str = LOCAL_DEVELOPMENT_OWNER_ID,
        now: Any | None = None,
        limit: int = _DEFAULT_CLEANUP_LIMIT,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        normalized_owner_id = _uuid(owner_id)
        removed = self.store.purge_expired(
            owner_id=normalized_owner_id,
            now=now,
            limit=limit,
        )

        def exists(candidate_owner: str, candidate_id: str) -> bool:
            if candidate_owner != normalized_owner_id:
                return True
            return (
                self.store.get_optional(
                    candidate_id,
                    owner_id=normalized_owner_id,
                    now=now,
                )
                is not None
            )

        try:
            self.drafts.cleanup_orphans(exists, limit=limit)
        except OSError:
            raise AttachmentServiceUnavailableError() from None
        return removed

    def cleanup_orphans(
        self,
        *,
        limit: int = _DEFAULT_CLEANUP_LIMIT,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        self._ensure_temp_directory()
        removed = 0
        try:
            candidates = sorted(
                self.temp_dir.iterdir(),
                key=lambda item: item.name,
            )
        except OSError:
            raise AttachmentServiceUnavailableError() from None
        for path in candidates:
            if removed >= limit:
                break
            if (
                path in self._active_paths
                or _MANAGED_NAME.fullmatch(path.name) is None
            ):
                continue
            if _safe_unlink(path, self.temp_dir):
                removed += 1
        return removed

    def _new_paths(self) -> tuple[Path, Path, Path]:
        token = uuid4_hex()
        return (
            self.temp_dir / f"{token}.source",
            self.temp_dir / f"{token}.job.json",
            self.temp_dir / f"{token}.result.json",
        )

    def _ensure_temp_directory(self) -> None:
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise AttachmentServiceUnavailableError() from None

    def _save_failure_after_cancellation(
        self,
        attachment_id: str,
        *,
        owner_id: str,
    ) -> None:
        try:
            self.store.save_failure(
                attachment_id,
                "attachment_service_unavailable",
                owner_id=owner_id,
            )
        except Exception:
            pass
        self._delete_draft_after_failure(
            attachment_id,
            owner_id=owner_id,
        )

    def _delete_draft_after_failure(
        self,
        attachment_id: str,
        *,
        owner_id: str,
    ) -> None:
        try:
            self.drafts.delete(
                attachment_id,
                owner_id=owner_id,
            )
        except OSError:
            pass

    def _hydrate_review(
        self,
        record: AttachmentRecord,
        *,
        owner_id: str,
    ) -> AttachmentRecord:
        if (
            record.status
            not in {"review_required", "confirmed", "bound"}
            or record.extracted_blocks
        ):
            return record
        result = self.drafts.load(record.id, owner_id=owner_id)
        if result is None:
            raise AttachmentServiceUnavailableError()
        return record.model_copy(update={"extracted_blocks": result.blocks})


class _CompletedUpload:
    def __init__(
        self,
        *,
        original_name: str,
        declared_media_type: str | None,
        size_bytes: int,
        sha256: str,
        signature: bytes,
    ) -> None:
        self.original_name = original_name
        self.declared_media_type = declared_media_type
        self.size_bytes = size_bytes
        self.sha256 = sha256
        self.signature = signature


class _MultipartFileWriter:
    def __init__(
        self,
        output: Any,
        *,
        max_file_bytes: int,
    ) -> None:
        self.output = output
        self.max_file_bytes = max_file_bytes
        self.part_count = 0
        self.headers: dict[bytes, bytes] = {}
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._headers_finished = False
        self._part_finished = False
        self._stream_finished = False
        self.original_name: str | None = None
        self.declared_media_type: str | None = None
        self.size_bytes = 0
        self.digest = hashlib.sha256()
        self.signature = bytearray()

    @property
    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    def on_part_begin(self) -> None:
        self.part_count += 1
        if self.part_count != 1:
            raise AttachmentInputError("attachment_corrupt")

    def on_header_field(
        self,
        data: bytes,
        start: int,
        end: int,
    ) -> None:
        self._header_field.extend(data[start:end])

    def on_header_value(
        self,
        data: bytes,
        start: int,
        end: int,
    ) -> None:
        self._header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        try:
            name = bytes(self._header_field).strip().lower()
            value = bytes(self._header_value).strip()
        finally:
            self._header_field.clear()
            self._header_value.clear()
        if not name or name in self.headers:
            raise AttachmentInputError("attachment_corrupt")
        self.headers[name] = value

    def on_headers_finished(self) -> None:
        disposition = self.headers.get(b"content-disposition")
        if disposition is None:
            raise AttachmentInputError("attachment_corrupt")
        try:
            disposition_type, options = parse_options_header(disposition)
        except Exception:
            raise AttachmentInputError("attachment_corrupt") from None
        if disposition_type.lower() != b"form-data":
            raise AttachmentInputError("attachment_corrupt")
        if options.get(b"name") != b"file":
            raise AttachmentInputError("attachment_corrupt")
        raw_filename = options.get(b"filename")
        if raw_filename is None:
            raise AttachmentInputError("attachment_corrupt")
        try:
            original_name = raw_filename.decode("utf-8")
            validate_attachment_name(original_name)
        except (UnicodeError, ValueError):
            raise AttachmentInputError(
                "attachment_name_invalid"
            ) from None

        raw_media_type = self.headers.get(b"content-type")
        declared_media_type: str | None = None
        if raw_media_type is not None:
            try:
                base_type, _ = parse_options_header(raw_media_type)
                declared_media_type = base_type.decode("ascii").lower()
            except (UnicodeError, ValueError):
                raise AttachmentInputError(
                    "attachment_type_unsupported"
                ) from None
            if not declared_media_type:
                raise AttachmentInputError(
                    "attachment_type_unsupported"
                )

        self.original_name = original_name
        self.declared_media_type = declared_media_type
        self._headers_finished = True

    def on_part_data(
        self,
        data: bytes,
        start: int,
        end: int,
    ) -> None:
        if not self._headers_finished or self._part_finished:
            raise AttachmentInputError("attachment_corrupt")
        chunk = data[start:end]
        if not chunk:
            return
        next_size = self.size_bytes + len(chunk)
        if next_size > self.max_file_bytes:
            raise AttachmentResourceLimitError("attachment_too_large")
        try:
            written = self.output.write(chunk)
        except OSError:
            raise AttachmentServiceUnavailableError() from None
        if written != len(chunk):
            raise AttachmentServiceUnavailableError()
        self.digest.update(chunk)
        if len(self.signature) < 16:
            remaining = 16 - len(self.signature)
            self.signature.extend(chunk[:remaining])
        self.size_bytes = next_size

    def on_part_end(self) -> None:
        if not self._headers_finished or self._part_finished:
            raise AttachmentInputError("attachment_corrupt")
        self._part_finished = True

    def on_end(self) -> None:
        self._stream_finished = True

    def complete(self) -> _CompletedUpload:
        if (
            self.part_count != 1
            or not self._headers_finished
            or not self._part_finished
            or not self._stream_finished
            or self.original_name is None
            or self.size_bytes <= 0
        ):
            raise AttachmentInputError("attachment_corrupt")
        return _CompletedUpload(
            original_name=self.original_name,
            declared_media_type=self.declared_media_type,
            size_bytes=self.size_bytes,
            sha256=self.digest.hexdigest(),
            signature=bytes(self.signature),
        )


async def _write_multipart_file(
    boundary: bytes,
    chunks: AsyncIterable[bytes],
    destination: Path,
    *,
    max_file_bytes: int,
) -> _CompletedUpload:
    try:
        with destination.open("xb") as output:
            writer = _MultipartFileWriter(
                output,
                max_file_bytes=max_file_bytes,
            )
            parser = MultipartParser(
                boundary,
                writer.callbacks,
                max_size=max_file_bytes + _MAX_MULTIPART_OVERHEAD,
                max_header_count=8,
                max_header_size=4224,
            )
            total_bytes = 0
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise AttachmentInputError("attachment_corrupt")
                total_bytes += len(chunk)
                if (
                    total_bytes
                    > max_file_bytes + _MAX_MULTIPART_OVERHEAD
                ):
                    raise AttachmentResourceLimitError(
                        "attachment_too_large"
                    )
                parser.write(chunk)
            parser.finalize()
            output.flush()
            return writer.complete()
    except AttachmentError:
        raise
    except asyncio.CancelledError:
        raise
    except OSError:
        raise AttachmentServiceUnavailableError() from None
    except Exception:
        raise AttachmentInputError("attachment_corrupt") from None


def _multipart_boundary(content_type: str | None) -> bytes:
    if content_type is None:
        raise AttachmentInputError("attachment_corrupt")
    try:
        media_type, options = parse_options_header(
            content_type.encode("ascii")
        )
    except (UnicodeError, ValueError):
        raise AttachmentInputError("attachment_corrupt") from None
    boundary = options.get(b"boundary")
    if media_type.lower() != b"multipart/form-data" or not boundary:
        raise AttachmentInputError("attachment_corrupt")
    if len(boundary) > 200 or any(
        value < 32 or value > 126 for value in boundary
    ):
        raise AttachmentInputError("attachment_corrupt")
    return boundary


def _detect_media_type(signature: bytes) -> AttachmentMediaType:
    if signature.startswith(_PDF_SIGNATURE):
        return "application/pdf"
    if signature.startswith(_PNG_SIGNATURE):
        return "image/png"
    if signature.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    raise AttachmentInputError("attachment_type_unsupported")


def _validate_declared_media_type(
    actual: AttachmentMediaType,
    declared: str | None,
) -> None:
    if declared is None:
        return
    if declared not in _ALLOWED_MEDIA_TYPES:
        raise AttachmentInputError("attachment_type_unsupported")
    if declared != actual:
        raise AttachmentInputError("attachment_type_mismatch")


def _validated_temp_directory(value: Path) -> Path:
    root = PROJECT_ROOT.resolve()
    temp_dir = Path(value).resolve()
    static_dir = (root / "app" / "web").resolve()
    test_staging_root = _is_test_staging_path(temp_dir, root)
    if (
        temp_dir == root
        or (
            not temp_dir.is_relative_to(root)
            and not test_staging_root
        )
        or temp_dir == static_dir
        or temp_dir.is_relative_to(static_dir)
    ):
        raise ValueError("附件临时目录必须是项目内非静态私有目录")
    if temp_dir.exists() and not temp_dir.is_dir():
        raise ValueError("附件临时目录不能指向普通文件")
    return temp_dir


def _uuid(value: object) -> str:
    from uuid import UUID

    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("ID 必须是有效 UUID") from exc


def _require_shared_temp_paths(
    source_path: Path,
    job_path: Path,
    result_path: Path,
) -> None:
    paths = tuple(Path(path).resolve(strict=False) for path in (
        source_path,
        job_path,
        result_path,
    ))
    if len({path.parent for path in paths}) != 1:
        raise ValueError("worker 文件必须位于同一受控目录")
    names = tuple(path.name for path in paths)
    if any(_MANAGED_NAME.fullmatch(name) is None for name in names):
        raise ValueError("worker 文件名无效")
    tokens = tuple(name.split(".", 1)[0] for name in names)
    if len(set(tokens)) != 1:
        raise ValueError("worker 文件令牌不一致")


def _read_worker_result(result_path: Path) -> Any:
    try:
        size = result_path.stat().st_size
        if size <= 0 or size > _MAX_WORKER_RESULT_BYTES:
            raise ValueError("invalid worker result size")
        return parse_worker_envelope(result_path.read_bytes())
    except Exception:
        raise AttachmentServiceUnavailableError() from None


def _worker_environment(temp_dir: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _WORKER_ENVIRONMENT_NAMES and value
    }
    environment["TEMP"] = str(temp_dir)
    environment["TMP"] = str(temp_dir)
    return environment


def _safe_unlink(path: Path, root: Path) -> bool:
    if (
        path.parent.resolve() != root
        or _MANAGED_NAME.fullmatch(path.name) is None
    ):
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def uuid4_hex() -> str:
    from uuid import uuid4

    return uuid4().hex
