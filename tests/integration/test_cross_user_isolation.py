from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.low_level import Type
from fastapi.testclient import TestClient

from app.attachments.models import ExtractionBlock, ExtractionResult
from app.attachments.service import AttachmentService
from app.auth.passwords import PasswordManager
from app.auth.service import AuthService
from app.auth.store import InMemoryAuthStore
from app.main import create_app
from app.privacy.policy import PrivacyPolicy
from tests.test_pipeline import make_pipeline


ORIGIN = "https://app.example.test"
NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
PASSWORD = "password-12345"


class _Mailer:
    def send_verification(self, **kwargs: Any) -> None:
        del kwargs

    def send_password_reset(self, **kwargs: Any) -> None:
        del kwargs


class _Captcha:
    def verify(self, *, token: str, remote_ip: str) -> bool:
        del token, remote_ip
        return True


class _ExtractionWorker:
    async def extract(
        self,
        source_path: Path,
        **kwargs: Any,
    ) -> ExtractionResult:
        del source_path, kwargs
        return ExtractionResult(
            media_type="application/pdf",
            page_count=1,
            extraction_method="direct_text",
            blocks=(
                ExtractionBlock(
                    page_number=1,
                    block_index=0,
                    text="订单金额 299 元",
                    confidence=0.99,
                ),
            ),
        )


def _application(tmp_path: Path):
    pipeline, _ = make_pipeline(tmp_path)
    settings = pipeline.settings.model_copy(
        update={
            "deployment_mode": "test",
            "public_base_url": ORIGIN,
            "cors_origins": ORIGIN,
            "privacy_policy_version": "2026-08-10",
            "attachment_temp_dir": tmp_path / "attachment-jobs",
        }
    )
    pipeline.settings = settings
    store = InMemoryAuthStore()
    passwords = PasswordManager(
        PasswordHasher(
            time_cost=1,
            memory_cost=8192,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )
    service = AuthService(
        store=store,
        passwords=passwords,
        mailer=_Mailer(),
        captcha=_Captcha(),
        policy=PrivacyPolicy(
            version="2026-08-10",
            text="测试隐私政策正文",
        ),
        public_base_url=ORIGIN,
        rate_limit_secret=b"a" * 32,
        now=lambda: NOW,
    )
    application = create_app(settings, pipeline=pipeline)
    application.state.auth_service = service
    application.state.attachment_store = pipeline.attachments
    application.state.attachment_service = AttachmentService(
        pipeline.attachments,
        temp_dir=settings.attachment_temp_path,
        max_file_bytes=settings.max_attachment_bytes,
        max_pdf_pages=settings.max_attachment_pdf_pages,
        max_image_pixels=settings.max_attachment_image_pixels,
        max_extracted_chars=settings.max_attachment_extracted_chars,
        low_confidence_threshold=(
            settings.attachment_low_confidence_threshold
        ),
        extraction_timeout_seconds=(
            settings.attachment_extraction_timeout_seconds
        ),
        worker=_ExtractionWorker(),
    )
    application.state.ocr_ready = True
    return application, service, store, passwords


def _authenticated_client(
    application: Any,
    service: AuthService,
    store: InMemoryAuthStore,
    passwords: PasswordManager,
    *,
    email: str,
    role: str,
) -> tuple[TestClient, dict[str, str], str]:
    decision = store.create_verified_user(
        email=email,
        password_hash=passwords.hash(PASSWORD),
        role=role,
        policy_version="2026-08-10",
        now=NOW,
    )
    assert decision.user is not None
    service.accept_privacy(
        user_id=decision.user.id,
        context="consultation",
        policy_version="2026-08-10",
    )
    login = service.login(
        email=email,
        password=PASSWORD,
        client_key=email,
    )
    csrf = service.issue_csrf(login.session_token)
    client = TestClient(application)
    client.cookies.set(
        application.state.settings.cookie_name,
        login.session_token,
    )
    return (
        client,
        {"Origin": ORIGIN, "X-CSRF-Token": csrf},
        decision.user.id,
    )


def _upload(client: TestClient, headers: dict[str, str]):
    return client.post(
        "/api/attachments",
        headers=headers,
        files={
            "file": (
                "order.pdf",
                b"%PDF-1.7\nowner-isolation",
                "application/pdf",
            )
        },
    )


def test_registered_users_and_admin_are_fully_owner_scoped(
    tmp_path: Path,
) -> None:
    application, service, auth_store, passwords = _application(tmp_path)
    user_a, headers_a, _ = _authenticated_client(
        application,
        service,
        auth_store,
        passwords,
        email="a@example.com",
        role="user",
    )
    admin_b, headers_b, _ = _authenticated_client(
        application,
        service,
        auth_store,
        passwords,
        email="b@example.com",
        role="admin",
    )

    created = user_a.post(
        "/api/consult",
        headers=headers_a,
        json={"message": "房东不退押金"},
    )
    assert created.status_code == 200
    session_id = created.json()["session_id"]
    random_session_id = str(uuid4())

    foreign_detail = admin_b.get(f"/api/sessions/{session_id}")
    missing_detail = admin_b.get(
        f"/api/sessions/{random_session_id}"
    )
    assert (
        foreign_detail.status_code,
        foreign_detail.json(),
    ) == (
        missing_detail.status_code,
        missing_detail.json(),
    )
    assert admin_b.get("/api/sessions").json() == {"sessions": []}

    foreign_continuation = admin_b.post(
        "/api/consult",
        headers=headers_b,
        json={
            "session_id": session_id,
            "message": "尝试读取别人的案件",
        },
    )
    missing_continuation = admin_b.post(
        "/api/consult",
        headers=headers_b,
        json={
            "session_id": random_session_id,
            "message": "尝试读取不存在的案件",
        },
    )
    assert (
        foreign_continuation.status_code,
        foreign_continuation.json(),
    ) == (
        missing_continuation.status_code,
        missing_continuation.json(),
    )

    upload = _upload(user_a, headers_a)
    assert upload.status_code == 200
    attachment_id = upload.json()["id"]
    random_attachment_id = str(uuid4())

    foreign_attachment = admin_b.get(
        f"/api/attachments/{attachment_id}"
    )
    missing_attachment = admin_b.get(
        f"/api/attachments/{random_attachment_id}"
    )
    assert (
        foreign_attachment.status_code,
        foreign_attachment.json(),
    ) == (
        missing_attachment.status_code,
        missing_attachment.json(),
    )

    foreign_confirm = admin_b.patch(
        f"/api/attachments/{attachment_id}",
        headers=headers_b,
        json={"confirmed_text": "不能确认别人的附件"},
    )
    missing_confirm = admin_b.patch(
        f"/api/attachments/{random_attachment_id}",
        headers=headers_b,
        json={"confirmed_text": "不能确认不存在的附件"},
    )
    assert (
        foreign_confirm.status_code,
        foreign_confirm.json(),
    ) == (
        missing_confirm.status_code,
        missing_confirm.json(),
    )

    assert admin_b.delete(
        f"/api/attachments/{attachment_id}",
        headers=headers_b,
    ).status_code == 204
    assert admin_b.delete(
        f"/api/attachments/{random_attachment_id}",
        headers=headers_b,
    ).status_code == 204
    assert user_a.get(
        f"/api/attachments/{attachment_id}"
    ).status_code == 200

    foreign_delete = admin_b.delete(
        f"/api/sessions/{session_id}",
        headers=headers_b,
    )
    missing_delete = admin_b.delete(
        f"/api/sessions/{random_session_id}",
        headers=headers_b,
    )
    assert foreign_delete.status_code == missing_delete.status_code == 204
    assert user_a.get(f"/api/sessions/{session_id}").status_code == 200
