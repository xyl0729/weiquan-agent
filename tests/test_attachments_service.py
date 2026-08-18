from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.attachments.errors import (
    AttachmentInputError,
    AttachmentResourceLimitError,
    AttachmentServiceUnavailableError,
)
from app.attachments.models import ExtractionBlock, ExtractionResult
from app.attachments.service import (
    AttachmentService,
    SubprocessExtractionWorker,
)
from app.attachments.store import AttachmentStore
from app.attachments.worker import WorkerLimits
from app.db.session import SessionStore


FIXTURES = Path(__file__).parent / "fixtures" / "attachments"
BOUNDARY = "phase4-boundary"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def _result(
    *,
    media_type: str = "application/pdf",
    text: str = "订单金额 299 元",
) -> ExtractionResult:
    dimensions = (
        {"width_px": 1200, "height_px": 500}
        if media_type != "application/pdf"
        else {}
    )
    return ExtractionResult(
        media_type=media_type,
        page_count=1,
        extraction_method=(
            "direct_text" if media_type == "application/pdf" else "ocr"
        ),
        blocks=(
            ExtractionBlock(
                page_number=1,
                block_index=0,
                text=text,
                confidence=1,
            ),
        ),
        **dimensions,
    )


class FakeWorker:
    def __init__(
        self,
        *,
        result: ExtractionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or _result()
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def extract(
        self,
        source_path: Path,
        *,
        declared_media_type: str | None,
        limits: WorkerLimits,
        job_path: Path,
        result_path: Path,
    ) -> ExtractionResult:
        self.calls.append(
            {
                "source_path": source_path,
                "source_bytes": source_path.read_bytes(),
                "declared_media_type": declared_media_type,
                "limits": limits,
                "job_path": job_path,
                "result_path": result_path,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


class BlocksStrippingStore:
    """Approximate the PostgreSQL adapter, which never stores OCR blocks."""

    def __init__(self, delegate: AttachmentStore) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    def save_extraction(
        self,
        attachment_id: str,
        result: ExtractionResult,
        *,
        owner_id: str,
    ) -> object:
        record = self.delegate.save_extraction(
            attachment_id,
            result,
            owner_id=owner_id,
        )
        return record.model_copy(update={"extracted_blocks": ()})

    def get(self, attachment_id: str, *, owner_id: str) -> object:
        record = self.delegate.get(attachment_id, owner_id=owner_id)
        return record.model_copy(update={"extracted_blocks": ()})

    def get_optional(
        self,
        attachment_id: str,
        *,
        owner_id: str,
        now: object | None = None,
    ) -> object:
        record = self.delegate.get_optional(
            attachment_id,
            owner_id=owner_id,
            now=now,
        )
        if record is None:
            return None
        return record.model_copy(update={"extracted_blocks": ()})

    def confirm(
        self,
        attachment_id: str,
        confirmed_text: str,
        *,
        owner_id: str,
    ) -> object:
        record = self.delegate.confirm(
            attachment_id,
            confirmed_text,
            owner_id=owner_id,
        )
        return record.model_copy(update={"extracted_blocks": ()})


class BlockingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(
        self,
        source_path: Path,
        *,
        declared_media_type: str | None,
        limits: WorkerLimits,
        job_path: Path,
        result_path: Path,
    ) -> ExtractionResult:
        del source_path, declared_media_type, limits, job_path, result_path
        self.started.set()
        await self.release.wait()
        return _result()


class ImmediateProcess:
    def __init__(self, return_code: int = 0) -> None:
        self.returncode: int | None = None
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self.return_code
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class StubbornProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exited.set()


def _stores(
    tmp_path: Path,
) -> tuple[SessionStore, AttachmentStore]:
    sessions = SessionStore(tmp_path / "app.db")
    sessions.initialize()
    return sessions, AttachmentStore(sessions)


def _service(
    tmp_path: Path,
    attachment_temp_dir: Path,
    *,
    worker: object | None = None,
    max_file_bytes: int = 10 * 1024 * 1024,
    extraction_timeout_seconds: float = 5,
) -> tuple[AttachmentService, SessionStore, AttachmentStore, Path]:
    sessions, store = _stores(tmp_path)
    temp_dir = attachment_temp_dir
    service = AttachmentService(
        store,
        temp_dir=temp_dir,
        max_file_bytes=max_file_bytes,
        max_pdf_pages=20,
        max_image_pixels=25_000_000,
        max_extracted_chars=200_000,
        low_confidence_threshold=0.75,
        extraction_timeout_seconds=extraction_timeout_seconds,
        worker=worker,
    )
    return service, sessions, store, temp_dir


def _part(
    *,
    name: str,
    data: bytes,
    filename: str | None = None,
    media_type: str | None = None,
) -> bytes:
    disposition = f'form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    headers = [f"Content-Disposition: {disposition}\r\n"]
    if media_type is not None:
        headers.append(f"Content-Type: {media_type}\r\n")
    return (
        f"--{BOUNDARY}\r\n".encode()
        + "".join(headers).encode("utf-8")
        + b"\r\n"
        + data
        + b"\r\n"
    )


def _multipart(*parts: bytes, close: bool = True) -> bytes:
    ending = f"--{BOUNDARY}--\r\n".encode() if close else b""
    return b"".join(parts) + ending


async def _chunks(
    value: bytes,
    *,
    chunk_size: int = 17,
) -> AsyncIterator[bytes]:
    for index in range(0, len(value), chunk_size):
        yield value[index : index + chunk_size]


def _managed_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(
        item
        for item in path.iterdir()
        if item.is_file() and not item.name.endswith(".ocr.json")
    )


def _sidecar_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(
        item
        for item in path.iterdir()
        if item.is_file() and item.name.endswith(".ocr.json")
    )


def _attachment_counts(sessions: SessionStore) -> dict[str, int]:
    with sessions.transaction() as connection:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM attachments
            GROUP BY status
            """
        ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def test_streamed_upload_uses_random_path_and_persists_exact_metadata(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    content = b"%PDF-1.7\nstreamed fixture bytes"
    worker = FakeWorker()
    service, _, store, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    body = _multipart(
        _part(
            name="file",
            filename="../../customer-contract.pdf",
            media_type="application/pdf",
            data=content,
        )
    )

    record = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )

    assert record.status == "review_required"
    assert record.original_name == "../../customer-contract.pdf"
    assert record.media_type == "application/pdf"
    assert record.size_bytes == len(content)
    assert record.sha256 == hashlib.sha256(content).hexdigest()
    assert store.get(record.id) == record
    assert len(worker.calls) == 1
    call = worker.calls[0]
    assert call["source_bytes"] == content
    assert call["declared_media_type"] == "application/pdf"
    assert call["source_path"].parent == temp_dir.resolve()
    assert "customer-contract" not in call["source_path"].name
    assert call["source_path"].name.endswith(".source")
    assert call["job_path"].parent == temp_dir.resolve()
    assert call["result_path"].parent == temp_dir.resolve()
    assert _managed_files(temp_dir) == []
    sidecars = _sidecar_files(temp_dir)
    assert len(sidecars) == 1
    if os.name != "nt":
        assert stat.S_IMODE(sidecars[0].stat().st_mode) == 0o600
    sidecar_text = sidecars[0].read_text(encoding="utf-8")
    assert record.id in sidecar_text
    assert record.original_name not in sidecar_text

    confirmed = service.confirm(record.id, "订单金额 399 元")

    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_text == "订单金额 399 元"
    assert _sidecar_files(temp_dir) == []


def test_sidecar_hydrates_postgres_style_upload_and_confirm_response(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    sessions, sqlite_store = _stores(tmp_path)
    del sessions
    store = BlocksStrippingStore(sqlite_store)
    service = AttachmentService(
        store,  # type: ignore[arg-type]
        temp_dir=project_attachment_temp_dir,
        max_file_bytes=10 * 1024 * 1024,
        max_pdf_pages=20,
        max_image_pixels=25_000_000,
        max_extracted_chars=200_000,
        low_confidence_threshold=0.75,
        extraction_timeout_seconds=5,
        worker=FakeWorker(),
    )
    body = _multipart(
        _part(
            name="file",
            filename="draft.pdf",
            media_type="application/pdf",
            data=b"%PDF-sidecar-hydration",
        )
    )

    uploaded = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )
    confirmed = service.confirm(uploaded.id, "订单金额 399 元")

    assert uploaded.extracted_blocks
    assert confirmed.extracted_blocks == uploaded.extracted_blocks
    assert confirmed.confirmed_text == "订单金额 399 元"
    assert _sidecar_files(project_attachment_temp_dir) == []


def test_missing_sidecar_does_not_confirm_postgres_style_record(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    _, sqlite_store = _stores(tmp_path)
    store = BlocksStrippingStore(sqlite_store)
    service = AttachmentService(
        store,  # type: ignore[arg-type]
        temp_dir=project_attachment_temp_dir,
        max_file_bytes=10 * 1024 * 1024,
        max_pdf_pages=20,
        max_image_pixels=25_000_000,
        max_extracted_chars=200_000,
        low_confidence_threshold=0.75,
        extraction_timeout_seconds=5,
        worker=FakeWorker(),
    )
    body = _multipart(
        _part(
            name="file",
            filename="draft.pdf",
            media_type="application/pdf",
            data=b"%PDF-missing-sidecar",
        )
    )
    uploaded = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )
    service.drafts.delete(uploaded.id)

    with pytest.raises(AttachmentServiceUnavailableError):
        service.confirm(uploaded.id, "不能写入")

    assert sqlite_store.get(uploaded.id).status == "review_required"


def test_byte_limit_stops_consuming_and_removes_partial_file(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    worker = FakeWorker()
    service, sessions, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
        max_file_bytes=32,
    )
    prefix = _part(
        name="file",
        filename="large.pdf",
        media_type="application/pdf",
        data=b"",
    )[:-2]
    yielded: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        chunks = (
            prefix + b"%PDF-" + b"A" * 64,
            b"B" * 64,
            b"\r\n" + f"--{BOUNDARY}--\r\n".encode(),
        )
        for index, chunk in enumerate(chunks):
            yielded.append(index)
            yield chunk

    with pytest.raises(AttachmentResourceLimitError) as caught:
        asyncio.run(service.process_multipart(CONTENT_TYPE, body()))

    assert caught.value.code == "attachment_too_large"
    assert yielded == [0]
    assert worker.calls == []
    assert _attachment_counts(sessions) == {}
    assert _managed_files(temp_dir) == []


@pytest.mark.parametrize(
    "body",
    [
        _multipart(
            _part(
                name="note",
                data=b"extra",
            ),
            _part(
                name="file",
                filename="valid.pdf",
                media_type="application/pdf",
                data=b"%PDF-valid",
            ),
        ),
        _multipart(
            _part(
                name="file",
                filename="first.pdf",
                media_type="application/pdf",
                data=b"%PDF-first",
            ),
            _part(
                name="file",
                filename="second.pdf",
                media_type="application/pdf",
                data=b"%PDF-second",
            ),
        ),
        _multipart(
            _part(
                name="wrong",
                filename="valid.pdf",
                media_type="application/pdf",
                data=b"%PDF-valid",
            )
        ),
        _multipart(
            _part(
                name="file",
                filename="truncated.pdf",
                media_type="application/pdf",
                data=b"%PDF-truncated",
            ),
            close=False,
        ),
    ],
    ids=["extra-field", "extra-file", "wrong-name", "truncated"],
)
def test_rejects_noncanonical_or_malformed_multipart(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    body: bytes,
) -> None:
    worker = FakeWorker()
    service, sessions, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )

    with pytest.raises(AttachmentInputError):
        asyncio.run(
            service.process_multipart(CONTENT_TYPE, _chunks(body))
        )

    assert worker.calls == []
    assert _attachment_counts(sessions) == {}
    assert _managed_files(temp_dir) == []


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "application/json",
        "multipart/form-data",
        "multipart/form-data; boundary=",
    ],
)
def test_rejects_missing_or_invalid_multipart_content_type(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    content_type: str | None,
) -> None:
    service, sessions, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )

    with pytest.raises(AttachmentInputError):
        asyncio.run(
            service.process_multipart(content_type, _chunks(b"invalid"))
        )

    assert _attachment_counts(sessions) == {}
    assert _managed_files(temp_dir) == []


def test_declared_type_mismatch_is_rejected_before_database_write(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    service, sessions, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )
    body = _multipart(
        _part(
            name="file",
            filename="mismatch.png",
            media_type="image/png",
            data=b"%PDF-not-an-image",
        )
    )

    with pytest.raises(AttachmentInputError) as caught:
        asyncio.run(
            service.process_multipart(CONTENT_TYPE, _chunks(body))
        )

    assert caught.value.code == "attachment_type_mismatch"
    assert _attachment_counts(sessions) == {}
    assert _managed_files(temp_dir) == []


def test_worker_safe_error_becomes_failed_record_and_cleans_all_files(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    worker = FakeWorker(
        error=AttachmentInputError("attachment_corrupt")
    )
    service, _, store, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    body = _multipart(
        _part(
            name="file",
            filename="broken.pdf",
            media_type="application/pdf",
            data=b"%PDF-broken",
        )
    )

    record = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )

    assert record.status == "failed"
    assert record.error_code == "attachment_corrupt"
    assert store.get(record.id) == record
    assert _managed_files(temp_dir) == []


def test_worker_media_type_mismatch_becomes_failed_record(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    worker = FakeWorker(result=_result(media_type="image/png"))
    service, _, store, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    body = _multipart(
        _part(
            name="file",
            filename="mismatched-worker.pdf",
            media_type="application/pdf",
            data=b"%PDF-valid",
        )
    )

    record = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )

    assert record.status == "failed"
    assert record.error_code == "attachment_type_mismatch"
    assert store.get(record.id) == record
    assert _managed_files(temp_dir) == []


def test_database_failure_still_cleans_source_job_and_result(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, store, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )
    body = _multipart(
        _part(
            name="file",
            filename="valid.pdf",
            media_type="application/pdf",
            data=b"%PDF-valid",
        )
    )

    def fail_save(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("injected database secret detail")

    monkeypatch.setattr(store, "save_extraction", fail_save)

    with pytest.raises(AttachmentServiceUnavailableError) as caught:
        asyncio.run(
            service.process_multipart(CONTENT_TYPE, _chunks(body))
        )

    assert caught.value.code == "attachment_service_unavailable"
    assert "injected" not in str(caught.value)
    assert "secret" not in str(caught.value).casefold()
    assert _managed_files(temp_dir) == []
    assert _sidecar_files(temp_dir) == []


def test_sensitive_upload_details_are_not_logged(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = "private-customer-contract.pdf"
    body_marker = "private-body-marker"
    prompt_marker = "private-prompt-marker"
    key_marker = "private-key-marker"
    monkeypatch.setenv("DEEPSEEK_API_KEY", key_marker)
    worker = FakeWorker(error=RuntimeError(prompt_marker))
    service, _, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    body = _multipart(
        _part(
            name="file",
            filename=filename,
            media_type="application/pdf",
            data=f"%PDF-{body_marker}".encode(),
        )
    )

    with caplog.at_level(logging.DEBUG):
        record = asyncio.run(
            service.process_multipart(CONTENT_TYPE, _chunks(body))
        )

    assert record.status == "failed"
    assert record.error_code == "attachment_service_unavailable"
    assert worker.calls
    source_path = str(worker.calls[0]["source_path"])
    for sensitive_value in (
        filename,
        body_marker,
        prompt_marker,
        key_marker,
        source_path,
    ):
        assert sensitive_value not in caplog.text
    assert _managed_files(temp_dir) == []


def test_parent_cancellation_marks_processing_failed_and_cleans_files(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    worker = BlockingWorker()
    service, sessions, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    body = _multipart(
        _part(
            name="file",
            filename="cancel.pdf",
            media_type="application/pdf",
            data=b"%PDF-cancel",
        )
    )

    async def cancel_during_extraction() -> None:
        task = asyncio.create_task(
            service.process_multipart(CONTENT_TYPE, _chunks(body))
        )
        await worker.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_extraction())

    assert _attachment_counts(sessions) == {"failed": 1}
    assert _managed_files(temp_dir) == []


def test_timeout_terminates_then_kills_stubborn_worker(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    process = StubbornProcess()

    async def process_factory(
        *args: object,
        **kwargs: object,
    ) -> StubbornProcess:
        del args, kwargs
        return process

    worker = SubprocessExtractionWorker(
        timeout_seconds=0.01,
        termination_grace_seconds=0.01,
        process_factory=process_factory,
    )
    service, _, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    body = _multipart(
        _part(
            name="file",
            filename="slow.pdf",
            media_type="application/pdf",
            data=b"%PDF-slow",
        )
    )

    record = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )

    assert record.status == "failed"
    assert record.error_code == "attachment_extraction_timeout"
    assert process.terminated is True
    assert process.killed is True
    assert _managed_files(temp_dir) == []


def test_invalid_worker_output_fails_closed_and_is_removed(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    async def process_factory(
        *args: object,
        **kwargs: object,
    ) -> ImmediateProcess:
        del kwargs
        Path(str(args[-1])).write_text(
            '{"status":"ok","result":{"unexpected":true}}',
            encoding="utf-8",
        )
        return ImmediateProcess()

    worker = SubprocessExtractionWorker(
        timeout_seconds=1,
        process_factory=process_factory,
    )
    service, _, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    body = _multipart(
        _part(
            name="file",
            filename="invalid-output.pdf",
            media_type="application/pdf",
            data=b"%PDF-invalid-worker-output",
        )
    )

    record = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )

    assert record.status == "failed"
    assert record.error_code == "attachment_service_unavailable"
    assert _managed_files(temp_dir) == []


def test_worker_process_receives_only_allowlisted_environment(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_environment: dict[str, str] = {}

    async def process_factory(
        *args: object,
        **kwargs: object,
    ) -> ImmediateProcess:
        captured_environment.update(kwargs["env"])
        result_path = Path(str(args[-1]))
        result_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "result": _result().model_dump(mode="json"),
                }
            ),
            encoding="utf-8",
        )
        return ImmediateProcess()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    monkeypatch.setenv("Authorization", "Bearer must-not-leak")
    monkeypatch.setenv("APP_PRIVATE_SECRET", "must-not-leak")
    worker = SubprocessExtractionWorker(
        timeout_seconds=1,
        process_factory=process_factory,
    )
    service, _, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    body = _multipart(
        _part(
            name="file",
            filename="safe.pdf",
            media_type="application/pdf",
            data=b"%PDF-safe",
        )
    )

    record = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )

    normalized_names = {name.upper() for name in captured_environment}
    assert record.status == "review_required"
    assert "DEEPSEEK_API_KEY" not in normalized_names
    assert "AUTHORIZATION" not in normalized_names
    assert "APP_PRIVATE_SECRET" not in normalized_names
    assert captured_environment["TEMP"] == str(temp_dir.resolve())
    assert captured_environment["TMP"] == str(temp_dir.resolve())
    assert _managed_files(temp_dir) == []


def test_default_worker_runs_selectable_pdf_in_real_subprocess(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    service, _, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        extraction_timeout_seconds=15,
    )
    content = (FIXTURES / "selectable.pdf").read_bytes()
    body = _multipart(
        _part(
            name="file",
            filename="selectable.pdf",
            media_type="application/pdf",
            data=content,
        )
    )

    record = asyncio.run(
        service.process_multipart(
            CONTENT_TYPE,
            _chunks(body, chunk_size=1021),
        )
    )

    assert record.status == "review_required"
    assert record.extraction_method == "direct_text"
    assert record.page_count == 2
    assert record.extracted_blocks
    assert _managed_files(temp_dir) == []


def test_default_worker_loads_local_ocr_in_real_subprocess(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    service, _, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        extraction_timeout_seconds=30,
    )
    content = (FIXTURES / "numeric.png").read_bytes()
    body = _multipart(
        _part(
            name="file",
            filename="numeric.png",
            media_type="image/png",
            data=content,
        )
    )

    record = asyncio.run(
        service.process_multipart(
            CONTENT_TYPE,
            _chunks(body, chunk_size=1021),
        )
    )

    extracted_text = " ".join(
        block.text for block in record.extracted_blocks
    )
    digits = "".join(
        character
        for character in extracted_text
        if character.isdecimal()
    )
    assert record.status == "review_required"
    assert record.media_type == "image/png"
    assert record.extraction_method == "ocr"
    assert "299" in digits
    assert _managed_files(temp_dir) == []


def test_recovery_fails_stale_processing_and_only_removes_managed_orphans(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    service, _, store, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )
    stale = store.create_processing(
        original_name="stale.pdf",
        media_type="application/pdf",
        size_bytes=10,
        sha256="a" * 64,
    )
    temp_dir.mkdir(parents=True)
    managed_names = (
        f"{uuid4().hex}.source",
        f"{uuid4().hex}.job.json",
        f"{uuid4().hex}.result.json",
    )
    for name in managed_names:
        (temp_dir / name).write_bytes(b"orphan")
    unrelated = temp_dir / "keep-me.txt"
    unrelated.write_text("not owned by attachment service", encoding="utf-8")
    outside = tmp_path / f"{uuid4().hex}.source"
    outside.write_bytes(b"outside")

    recovered, removed = service.recover()

    restored = store.get(stale.id)
    assert recovered == 1
    assert removed == 3
    assert restored.status == "failed"
    assert restored.error_code == "attachment_service_unavailable"
    assert unrelated.exists()
    assert outside.exists()
    assert sorted(path.name for path in _managed_files(temp_dir)) == [
        "keep-me.txt"
    ]


def test_delete_and_startup_recovery_remove_unconfirmed_sidecars(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    service, _, store, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )
    body = _multipart(
        _part(
            name="file",
            filename="draft.pdf",
            media_type="application/pdf",
            data=b"%PDF-draft",
        )
    )
    first = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )
    assert len(_sidecar_files(temp_dir)) == 1

    service.delete(first.id)

    assert store.get_optional(first.id) is None
    assert _sidecar_files(temp_dir) == []

    second = asyncio.run(
        service.process_multipart(CONTENT_TYPE, _chunks(body))
    )
    assert len(_sidecar_files(temp_dir)) == 1

    recovered, removed = service.recover()

    assert recovered == 1
    assert removed == 1
    assert store.get(second.id).status == "failed"
    assert _sidecar_files(temp_dir) == []
