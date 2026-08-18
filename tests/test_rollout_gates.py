from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth.errors import (
    RegistrationCapacityError,
    RegistrationClosedError,
)
from app.auth.passwords import PasswordManager
from app.auth.service import AuthService
from app.auth.store import InMemoryAuthStore
from app.config import Settings
from app.main import create_app
from app.privacy.policy import PrivacyPolicy
from tests.test_pipeline import make_pipeline


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
ORIGIN = "http://testserver"
PASSWORD = "password-12345"


class RecordingMailer:
    def __init__(self) -> None:
        self.verification: list[tuple[str, str]] = []

    def send_verification(
        self,
        *,
        to_email: str,
        verification_code: str,
    ) -> None:
        self.verification.append((to_email, verification_code))

    def send_password_reset(self, **kwargs: Any) -> None:
        del kwargs


class RecordingCaptcha:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def verify(self, *, token: str, remote_ip: str) -> bool:
        self.calls.append((token, remote_ip))
        return True


def _passwords() -> PasswordManager:
    return PasswordManager(
        PasswordHasher(
            time_cost=1,
            memory_cost=8192,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )


def _service(
    *,
    rollout_stage: str,
    capacity: int = 100,
    now: Any = None,
) -> tuple[
    AuthService,
    InMemoryAuthStore,
    RecordingMailer,
    RecordingCaptcha,
]:
    store = InMemoryAuthStore(capacity_limit=capacity)
    mailer = RecordingMailer()
    captcha = RecordingCaptcha()
    service = AuthService(
        store=store,
        passwords=_passwords(),
        mailer=mailer,
        captcha=captcha,
        policy=PrivacyPolicy(
            version="2026-08-10",
            text="测试隐私政策正文",
        ),
        public_base_url=ORIGIN,
        rate_limit_secret=b"r" * 32,
        now=now or (lambda: NOW),
        rollout_stage=rollout_stage,  # type: ignore[arg-type]
        invited_user_limit=10,
    )
    return service, store, mailer, captcha


def _register(service: AuthService, email: str) -> None:
    service.register(
        email=email,
        password=PASSWORD,
        captcha_token="captcha-ok",
        privacy_version="2026-08-10",
        privacy_accepted=True,
        client_key=email,
    )


@pytest.mark.parametrize("stage", ["internal", "invited"])
def test_non_public_stages_reject_registration_before_sensitive_work(
    stage: str,
) -> None:
    def unexpected_clock() -> datetime:
        raise AssertionError("closed registration must not read the clock")

    service, store, mailer, captcha = _service(
        rollout_stage=stage,
        now=unexpected_clock,
    )

    with pytest.raises(RegistrationClosedError):
        _register(service, "closed@example.test")

    assert store.list_users() == ()
    assert mailer.verification == []
    assert captcha.calls == []


def test_registration_closed_has_a_stable_public_api_error(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deployment_mode="test",
        rollout_stage="internal",
        public_base_url=ORIGIN,
        cors_origins=ORIGIN,
        privacy_policy_version="2026-08-10",
        db_path=tmp_path / "rollout.db",
        attachment_temp_dir=tmp_path / "attachments",
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/auth/register",
            headers={"Origin": ORIGIN},
            json={
                "email": "closed@example.com",
                "password": PASSWORD,
                "captcha_token": "not-consumed",
                "privacy_version": "2026-08-10",
                "privacy_accepted": True,
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == (
        "public_registration_closed"
    )


def test_invited_limit_is_atomic_and_counts_the_admin_slot() -> None:
    service, store, mailer, _ = _service(rollout_stage="invited")
    service.create_admin_account(
        email="admin@example.com",
        password=PASSWORD,
        privacy_version="2026-08-10",
        privacy_accepted=True,
    )

    for index in range(9):
        result = service.invite_user(
            email=f"invite-{index}@example.com",
            password=PASSWORD,
            privacy_version="2026-08-10",
            privacy_accepted=True,
        )
        assert result.created is True

    with pytest.raises(RegistrationCapacityError):
        service.invite_user(
            email="invite-10@example.com",
            password=PASSWORD,
            privacy_version="2026-08-10",
            privacy_accepted=True,
        )

    assert store.capacity_used(now=NOW) == 10
    assert len(mailer.verification) == 9


def test_public_stage_uses_the_global_registration_capacity() -> None:
    service, store, mailer, captcha = _service(
        rollout_stage="public",
        capacity=2,
    )

    _register(service, "one@example.com")
    _register(service, "two@example.com")
    with pytest.raises(RegistrationCapacityError):
        _register(service, "three@example.com")

    assert store.capacity_used(now=NOW) == 2
    assert len(mailer.verification) == 2
    assert len(captcha.calls) == 3


def _production_values(tmp_path: Path) -> dict[str, object]:
    statutes = tmp_path / "statutes.db"
    statutes.write_bytes(b"sqlite-placeholder")
    return {
        "_env_file": None,
        "deployment_mode": "production",
        "rollout_stage": "public",
        "database_url": (
            "postgresql+psycopg://weiquan:database-secret@"
            "postgres:5432/weiquan"
        ),
        "public_base_url": "https://weiquan.example.test",
        "cors_origins": "https://weiquan.example.test",
        "cookie_secure": True,
        "llm_provider": "deepseek",
        "key_mode": "server",
        "deepseek_api_key": "deepseek-secret-value",
        "session_secret": "session-secret-value-with-at-least-32-bytes",
        "ip_hmac_secret": "ip-hmac-secret-value-with-at-least-32-bytes",
        "aliyun_access_key_id": "aliyun-access-key-id",
        "aliyun_access_key_secret": "aliyun-access-key-secret",
        "directmail_account_name": "notice@example.test",
        "oss_endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "oss_bucket": "weiquan-private-test",
        "deletion_manifest_recipient": "age1productionrecipient",
        "privacy_policy_version": "2026-08-10",
        "statutes_db_path": statutes,
        "attachment_temp_dir": tmp_path / "private" / "attachments",
        "log_dir": tmp_path / "private" / "logs",
        "backup_staging_dir": tmp_path / "private" / "backup-staging",
    }


def test_production_public_stage_requires_explicit_launch_approval(
    tmp_path: Path,
) -> None:
    values = _production_values(tmp_path)

    with pytest.raises(ValidationError) as caught:
        Settings(**values)

    assert "PUBLIC_LAUNCH_APPROVED" in str(caught.value)
    values["public_launch_approved"] = True
    settings = Settings(**values)
    assert settings.rollout_stage == "public"


def test_manual_pause_blocks_only_new_work(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    settings = Settings(
        _env_file=None,
        deployment_mode="test",
        rollout_stage="public",
        public_base_url=ORIGIN,
        cors_origins=ORIGIN,
        privacy_policy_version="2026-08-10",
        db_path=tmp_path / "rollout.db",
        statutes_db_path=pipeline.settings.statutes_db_path,
        attachment_temp_dir=tmp_path / "attachments",
    )
    pipeline.settings = settings
    service, _, _, _ = _service(rollout_stage="public")
    user = service.create_admin_account(
        email="operator@example.com",
        password=PASSWORD,
        privacy_version="2026-08-10",
        privacy_accepted=True,
    )
    service.accept_privacy(
        user_id=user.id,
        context="consultation",
        policy_version="2026-08-10",
    )
    application = create_app(settings, pipeline=pipeline)
    application.state.auth_service = service

    with TestClient(application) as client:
        login = client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={
                "email": user.email,
                "password": PASSWORD,
            },
        )
        assert login.status_code == 200
        csrf = client.get("/api/auth/csrf").json()["csrf_token"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        created = client.post(
            "/api/consult",
            headers=headers,
            json={"message": "房东无理由扣除押金"},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        paused = settings.model_copy(
            update={"new_work_enabled": False}
        )
        application.state.settings = paused
        pipeline.settings = paused

        for response in (
            client.post(
                "/api/consult",
                headers=headers,
                json={"message": "暂停期间的新咨询"},
            ),
            client.post(
                "/api/trial/consult",
                headers={"Origin": ORIGIN},
                json={"message": "暂停期间的匿名咨询"},
            ),
            client.post(
                "/api/attachments",
                headers=headers,
                files={
                    "file": (
                        "paused.pdf",
                        b"%PDF-paused",
                        "application/pdf",
                    )
                },
            ),
        ):
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "new_work_paused"

        listed = client.get("/api/sessions")
        detail = client.get(f"/api/sessions/{session_id}")
        assert listed.status_code == 200
        assert detail.status_code == 200

        assert client.post(
            "/api/auth/logout",
            headers=headers,
        ).status_code == 204
        relogin = client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={
                "email": user.email,
                "password": PASSWORD,
            },
        )
        assert relogin.status_code == 200
        next_csrf = client.get("/api/auth/csrf").json()["csrf_token"]
        assert client.delete(
            f"/api/sessions/{session_id}",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": next_csrf,
            },
        ).status_code == 204
