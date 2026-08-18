from __future__ import annotations

from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.low_level import Type

from app.auth.passwords import PasswordManager
from app.auth.service import AuthService
from app.auth.store import InMemoryAuthStore
from app.privacy.policy import PrivacyPolicy, load_privacy_policy


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


class Mailer:
    def send_verification(self, **kwargs: str) -> None:
        del kwargs

    def send_password_reset(self, **kwargs: str) -> None:
        del kwargs


class Captcha:
    def verify(self, **kwargs: str) -> bool:
        del kwargs
        return True


def _service(
    store: InMemoryAuthStore,
    *,
    version: str,
) -> AuthService:
    return AuthService(
        store=store,
        passwords=PasswordManager(
            PasswordHasher(
                time_cost=1,
                memory_cost=8192,
                parallelism=1,
                type=Type.ID,
            )
        ),
        mailer=Mailer(),
        captcha=Captcha(),
        policy=PrivacyPolicy(version=version, text="隐私正文"),
        public_base_url="https://weiquan.example.test",
        rate_limit_secret=b"p" * 32,
        now=lambda: NOW,
    )


def test_bundled_chinese_privacy_policy_is_complete() -> None:
    policy = load_privacy_policy(version="2026-08-10")

    assert policy.version == "2026-08-10"
    assert "维权咨询助手" in policy.text
    assert "DeepSeek" in policy.text
    assert "30 天" in policy.text
    assert "TODO" not in policy.text
    assert "TBD" not in policy.text


def test_registration_and_consultation_acceptances_are_separate() -> None:
    store = InMemoryAuthStore()
    service = _service(store, version="2026-08-10")
    result = service.register(
        email="user@example.com",
        password="password-12345",
        captcha_token="ok",
        privacy_version="2026-08-10",
        privacy_accepted=True,
        client_key="client",
    )

    assert store.has_privacy_acceptance(
        user_id=result.user.id,
        context="registration",
        policy_version="2026-08-10",
    )
    assert service.requires_privacy_acceptance(
        user_id=result.user.id,
        context="consultation",
    )

    service.accept_privacy(
        user_id=result.user.id,
        context="consultation",
        policy_version="2026-08-10",
    )
    assert not service.requires_privacy_acceptance(
        user_id=result.user.id,
        context="consultation",
    )


def test_new_policy_version_requires_a_new_consultation_acceptance() -> None:
    store = InMemoryAuthStore()
    old = _service(store, version="2026-08-10")
    result = old.register(
        email="user@example.com",
        password="password-12345",
        captcha_token="ok",
        privacy_version="2026-08-10",
        privacy_accepted=True,
        client_key="client",
    )
    old.accept_privacy(
        user_id=result.user.id,
        context="consultation",
        policy_version="2026-08-10",
    )
    updated = _service(store, version="2026-09-01")

    assert updated.requires_privacy_acceptance(
        user_id=result.user.id,
        context="consultation",
    )

