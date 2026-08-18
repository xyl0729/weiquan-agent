from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.agent.errors import ProviderError, ProviderOutputError
from app.attachments.errors import AttachmentServiceUnavailableError
from app.execution.bounded import BoundedExecutor
from app.integrations.captcha import (
    AliyunCaptchaVerifier,
    CaptchaVerificationError,
)
from app.integrations.directmail import (
    AliyunDirectMailSender,
    MailDeliveryError,
)
from app.integrations.oss import (
    AliyunPrivateObjectStore,
    ObjectStorageError,
)
from app.providers.deepseek import DeepSeekProvider
from tests.test_aliyun_integrations import (
    CaptchaClient,
    DirectMailClient,
)
from tests.test_attachments_service import (
    CONTENT_TYPE,
    BlockingWorker,
    FakeWorker,
    _chunks,
    _managed_files,
    _multipart,
    _part,
    _service,
    _sidecar_files,
)
from tests.test_providers import (
    EXTRACTION_CONTEXT,
    deepseek_response,
    make_client,
)


pytestmark = pytest.mark.integration


def _provider_body() -> bytes:
    return _multipart(
        _part(
            name="file",
            filename="fault.pdf",
            media_type="application/pdf",
            data=b"%PDF-failure-matrix",
        )
    )


async def _extract_with_handler(
    handler,
) -> object:
    async with make_client(handler) as client:
        provider = DeepSeekProvider(
            api_key="mock-secret",
            client=client,
            max_retries=0,
        )
        return await provider.extract_facts(
            "房东扣押金",
            EXTRACTION_CONTEXT,
        )


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (429, "provider_rate_limited"),
        (500, "provider_server_error"),
        (503, "provider_server_error"),
    ],
)
def test_deepseek_http_failures_are_safely_classified(
    status_code: int,
    category: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    with pytest.raises(ProviderError) as caught:
        asyncio.run(_extract_with_handler(handler))

    assert caught.value.category == category
    assert "mock-secret" not in str(caught.value)


def test_deepseek_timeout_and_invalid_output_fail_closed() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            "private-upstream-detail",
            request=request,
        )

    with pytest.raises(ProviderError) as timeout:
        asyncio.run(_extract_with_handler(timeout_handler))
    assert timeout.value.category == "provider_timeout"
    assert "private-upstream-detail" not in str(timeout.value)

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        del request
        return deepseek_response("not-json")

    with pytest.raises(ProviderOutputError):
        asyncio.run(_extract_with_handler(invalid_handler))


def test_aliyun_sdk_failures_never_expose_vendor_details() -> None:
    marker = "aliyun-sdk-private-detail"
    mail = AliyunDirectMailSender(
        client=DirectMailClient(error=RuntimeError(marker)),
        account_name="notice@example.test",
        from_alias="维权咨询助手",
    )
    captcha = AliyunCaptchaVerifier(
        client=CaptchaClient(error=RuntimeError(marker)),
        scene_id="scene-id",
    )

    with pytest.raises(MailDeliveryError) as mail_error:
        mail.send_verification(
            to_email="user@example.test",
            verification_code="123456",
        )
    with pytest.raises(CaptchaVerificationError) as captcha_error:
        captcha.verify(
            token="captcha-token",
            remote_ip="198.51.100.1",
        )

    assert marker not in str(mail_error.value)
    assert marker not in str(captcha_error.value)


def test_oss_sdk_failure_is_safe_and_keeps_objects_private() -> None:
    marker = "oss-sdk-private-detail"

    class FailingBucket:
        def put_object(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError(marker)

    store = AliyunPrivateObjectStore(FailingBucket())

    with pytest.raises(ObjectStorageError) as caught:
        store.put_private_object(
            "deletions/manifest.age",
            b"encrypted-payload",
            content_type="application/octet-stream",
        )

    assert marker not in str(caught.value)


def test_ocr_queue_full_returns_busy_without_leaking_jobs(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    worker = BlockingWorker()
    service, _, _, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )
    service.executor = BoundedExecutor(
        name="ocr",
        max_concurrency=1,
        max_waiting=0,
    )

    async def exercise() -> tuple[object, object]:
        first_task = asyncio.create_task(
            service.process_multipart(
                CONTENT_TYPE,
                _chunks(_provider_body()),
            )
        )
        await worker.started.wait()
        second = await service.process_multipart(
            CONTENT_TYPE,
            _chunks(_provider_body()),
        )
        worker.release.set()
        first = await first_task
        return first, second

    first, second = asyncio.run(exercise())

    assert first.status == "review_required"
    assert second.status == "failed"
    assert second.error_code == "attachment_service_busy"
    service.delete(first.id)
    assert _managed_files(temp_dir) == []
    assert _sidecar_files(temp_dir) == []
    assert service.executor.snapshot().running == 0
    assert service.executor.snapshot().waiting == 0


def test_database_persistence_failure_is_safely_mapped_and_cleaned(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "postgres-connection-private-detail"
    service, _, store, temp_dir = _service(
        tmp_path,
        project_attachment_temp_dir,
        worker=FakeWorker(),
    )

    def fail_save(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(store, "save_extraction", fail_save)

    with pytest.raises(AttachmentServiceUnavailableError) as caught:
        asyncio.run(
            service.process_multipart(
                CONTENT_TYPE,
                _chunks(_provider_body()),
            )
        )

    assert marker not in str(caught.value)
    assert _managed_files(temp_dir) == []
    assert _sidecar_files(temp_dir) == []
