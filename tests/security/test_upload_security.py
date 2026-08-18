from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.attachments.errors import (
    AttachmentInputError,
    AttachmentServiceUnavailableError,
)
from tests.test_attachments_service import (
    CONTENT_TYPE,
    BlockingWorker,
    FakeWorker,
    _attachment_counts,
    _chunks,
    _managed_files,
    _multipart,
    _part,
    _service,
    _sidecar_files,
)


def _pdf_body(filename: str = "upload.pdf") -> bytes:
    return _multipart(
        _part(
            name="file",
            filename=filename,
            media_type="application/pdf",
            data=b"%PDF-security-test",
        )
    )


def test_traversal_filename_cannot_control_managed_path(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    worker = FakeWorker()
    service, _, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )

    record = asyncio.run(
        service.process_multipart(
            CONTENT_TYPE,
            _chunks(_pdf_body("../../private-contract.pdf")),
        )
    )

    source_path = worker.calls[0]["source_path"]
    assert record.status == "review_required"
    assert source_path.parent == temp_dir.resolve()
    assert "private-contract" not in source_path.name
    assert source_path.name.endswith(".source")
    assert _managed_files(temp_dir) == []


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (
            _multipart(
                _part(
                    name="file",
                    filename="mismatch.png",
                    media_type="image/png",
                    data=b"%PDF-not-a-png",
                )
            ),
            "attachment_type_mismatch",
        ),
        (
            _multipart(
                _part(
                    name="file",
                    filename="truncated.pdf",
                    media_type="application/pdf",
                    data=b"%PDF-truncated",
                ),
                close=False,
            ),
            "attachment_corrupt",
        ),
    ],
)
def test_mime_mismatch_and_malformed_multipart_fail_before_persistence(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    body: bytes,
    expected_code: str,
) -> None:
    service, sessions, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )

    with pytest.raises(AttachmentInputError) as caught:
        asyncio.run(
            service.process_multipart(CONTENT_TYPE, _chunks(body))
        )

    assert caught.value.code == expected_code
    assert _attachment_counts(sessions) == {}
    assert _managed_files(temp_dir) == []
    assert _sidecar_files(temp_dir) == []


def test_read_only_upload_storage_maps_to_safe_error(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )
    original_open = Path.open

    def deny_source_create(
        path: Path,
        *args: object,
        **kwargs: object,
    ):
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        if (
            path.parent.resolve() == temp_dir.resolve()
            and path.name.endswith(".source")
            and "x" in mode
        ):
            raise PermissionError("simulated read-only storage")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_source_create)

    with pytest.raises(AttachmentServiceUnavailableError) as caught:
        asyncio.run(
            service.process_multipart(
                CONTENT_TYPE,
                _chunks(_pdf_body()),
            )
        )

    assert "read-only" not in str(caught.value)
    assert _attachment_counts(sessions) == {}
    assert _managed_files(temp_dir) == []


def test_cancellation_and_total_timeout_release_all_temporary_files(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    cancel_db = tmp_path / "cancel-db"
    cancel_db.mkdir()
    cancel_worker = BlockingWorker()
    cancel_service, cancel_sessions, _, cancel_temp = _service(
        cancel_db,
        project_attachment_temp_dir / "cancel",
        worker=cancel_worker,
    )

    async def cancel_upload() -> None:
        task = asyncio.create_task(
            cancel_service.process_multipart(
                CONTENT_TYPE,
                _chunks(_pdf_body("cancel.pdf")),
            )
        )
        await cancel_worker.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_upload())

    timeout_db = tmp_path / "timeout-db"
    timeout_db.mkdir()
    timeout_worker = BlockingWorker()
    timeout_service, timeout_sessions, _, timeout_temp = _service(
        timeout_db,
        project_attachment_temp_dir / "timeout",
        worker=timeout_worker,
        extraction_timeout_seconds=0.01,
    )
    timed_out = asyncio.run(
        timeout_service.process_multipart(
            CONTENT_TYPE,
            _chunks(_pdf_body("timeout.pdf")),
        )
    )

    assert _attachment_counts(cancel_sessions) == {"failed": 1}
    assert timed_out.status == "failed"
    assert timed_out.error_code == "attachment_extraction_timeout"
    assert _attachment_counts(timeout_sessions) == {"failed": 1}
    assert _managed_files(cancel_temp) == []
    assert _managed_files(timeout_temp) == []
    assert _sidecar_files(cancel_temp) == []
    assert _sidecar_files(timeout_temp) == []


def test_persistence_failure_removes_draft_and_redacts_database_error(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, store, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )

    def fail_save(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("postgres-password-private-marker")

    monkeypatch.setattr(store, "save_extraction", fail_save)

    with pytest.raises(AttachmentServiceUnavailableError) as caught:
        asyncio.run(
            service.process_multipart(
                CONTENT_TYPE,
                _chunks(_pdf_body("database.pdf")),
            )
        )

    assert "postgres-password-private-marker" not in str(caught.value)
    assert caught.value.code == "attachment_service_unavailable"
    assert _managed_files(temp_dir) == []
    assert _sidecar_files(temp_dir) == []
