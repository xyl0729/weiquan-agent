from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type

from app.auth.errors import (
    AuthenticationFailedError,
    MailUnavailableError,
    RegistrationCapacityError,
    TokenInvalidError,
    VerificationCodeInvalidError,
)
from app.auth.passwords import PasswordManager
from app.auth.service import AuthService
from app.auth.store import InMemoryAuthStore
from app.privacy.policy import PrivacyPolicy


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        return self.current


class RecordingMailer:
    def __init__(self) -> None:
        self.verification: list[tuple[str, str]] = []
        self.password_reset: list[tuple[str, str]] = []
        self.fail_verification = False

    def send_verification(
        self,
        *,
        to_email: str,
        verification_code: str,
    ) -> None:
        if self.fail_verification:
            raise RuntimeError("injected mail failure")
        self.verification.append((to_email, verification_code))

    def send_password_reset(
        self,
        *,
        to_email: str,
        reset_url: str,
    ) -> None:
        self.password_reset.append((to_email, reset_url))


class PassingCaptcha:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def verify(self, *, token: str, remote_ip: str) -> bool:
        self.calls.append((token, remote_ip))
        return token == "captcha-ok"


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
    capacity: int = 100,
    clock: Clock | None = None,
    mailer: RecordingMailer | None = None,
) -> tuple[
    AuthService,
    InMemoryAuthStore,
    RecordingMailer,
    PassingCaptcha,
    Clock,
]:
    active_clock = clock or Clock()
    store = InMemoryAuthStore(capacity_limit=capacity)
    active_mailer = mailer or RecordingMailer()
    captcha = PassingCaptcha()
    service = AuthService(
        store=store,
        passwords=_passwords(),
        mailer=active_mailer,
        captcha=captcha,
        policy=PrivacyPolicy(
            version="2026-08-10",
            text="测试隐私政策正文",
        ),
        public_base_url="https://weiquan.example.test",
        rate_limit_secret=b"r" * 32,
        now=active_clock,
    )
    return service, store, active_mailer, captcha, active_clock


def _register(
    service: AuthService,
    email: str = "User@Example.COM ",
    password: str = "password-12345",
) -> None:
    service.register(
        email=email,
        password=password,
        captcha_token="captcha-ok",
        privacy_version="2026-08-10",
        privacy_accepted=True,
        client_key="198.51.100.10",
    )


def _token_from_url(url: str) -> str:
    return parse_qs(urlsplit(url).fragment)["token"][0]


def _activate(
    service: AuthService,
    mailer: RecordingMailer,
    *,
    email: str = "user@example.com",
    password: str = "password-12345",
) -> str:
    _register(service, email=email, password=password)
    code = mailer.verification[-1][1]
    return service.verify_email(
        email=email,
        code=code,
        client_key="198.51.100.10",
    ).id


def test_pending_duplicate_resends_code_without_taking_another_slot() -> None:
    service, store, mailer, captcha, _ = _service(capacity=2)

    _register(service)
    _register(service, email=" user@example.com")

    user = store.get_user_by_email("user@example.com")
    assert user is not None
    assert user.email == "user@example.com"
    assert store.capacity_used(now=NOW) == 1
    assert len(mailer.verification) == 2
    assert mailer.verification[0][1] != mailer.verification[1][1]
    assert len(captcha.calls) == 2
    assert store.has_privacy_acceptance(
        user_id=user.id,
        context="registration",
        policy_version="2026-08-10",
    )


def test_capacity_full_never_calls_mailer_and_expired_pending_releases_slot() -> None:
    service, store, mailer, _, clock = _service(capacity=1)
    _register(service, email="first@example.com")

    with pytest.raises(RegistrationCapacityError):
        _register(service, email="second@example.com")
    assert len(mailer.verification) == 1

    clock.current += timedelta(hours=24, seconds=1)
    _register(service, email="second@example.com")

    assert store.capacity_used(now=clock.current) == 1
    assert len(mailer.verification) == 2


def test_verification_code_is_one_time_expires_and_resend_invalidates_old() -> None:
    service, _, mailer, _, clock = _service()
    _register(service)
    first = mailer.verification[-1][1]

    service.resend_verification(
        email="user@example.com",
        client_key="198.51.100.11",
    )
    second = mailer.verification[-1][1]

    with pytest.raises(VerificationCodeInvalidError):
        service.verify_email(
            email="user@example.com",
            code=first,
            client_key="198.51.100.12",
        )
    service.verify_email(
        email="USER@example.com",
        code=second,
        client_key="198.51.100.12",
    )
    with pytest.raises(VerificationCodeInvalidError):
        service.verify_email(
            email="user@example.com",
            code=second,
            client_key="198.51.100.12",
        )

    _register(service, email="late@example.com")
    expired = mailer.verification[-1][1]
    clock.current += timedelta(minutes=10, seconds=1)
    with pytest.raises(VerificationCodeInvalidError):
        service.verify_email(
            email="late@example.com",
            code=expired,
            client_key="198.51.100.13",
        )


def test_mail_failure_keeps_pending_account_for_limited_resend() -> None:
    mailer = RecordingMailer()
    mailer.fail_verification = True
    service, store, _, _, _ = _service(mailer=mailer)

    with pytest.raises(MailUnavailableError):
        _register(service)

    pending = store.get_user_by_email("user@example.com")
    assert pending is not None
    assert pending.status == "pending_verification"

    mailer.fail_verification = False
    _register(service, email="user@example.com")
    assert len(mailer.verification) == 1


def test_verification_code_cannot_be_used_for_another_email() -> None:
    service, _, mailer, _, _ = _service()
    _register(service, email="first@example.com")
    first_code = mailer.verification[-1][1]
    _register(service, email="second@example.com")

    with pytest.raises(VerificationCodeInvalidError):
        service.verify_email(
            email="second@example.com",
            code=first_code,
            client_key="198.51.100.21",
        )


def test_login_uses_one_generic_failure_and_session_is_revocable() -> None:
    service, _, mailer, _, _ = _service()
    _activate(service, mailer)

    failures = []
    for email, password in (
        ("missing@example.com", "password-12345"),
        ("user@example.com", "wrong-password"),
    ):
        with pytest.raises(AuthenticationFailedError) as caught:
            service.login(
                email=email,
                password=password,
                client_key=email,
            )
        failures.append((caught.value.code, caught.value.safe_message))
    assert failures[0] == failures[1]

    login = service.login(
        email="user@example.com",
        password="password-12345",
        client_key="198.51.100.30",
    )
    assert service.authenticate(login.session_token).user.id == login.user.id

    service.logout(login.session_token)
    with pytest.raises(AuthenticationFailedError):
        service.authenticate(login.session_token)


def test_password_reset_is_single_use_and_revokes_all_sessions() -> None:
    service, _, mailer, _, _ = _service()
    _activate(service, mailer)
    first_session = service.login(
        email="user@example.com",
        password="password-12345",
        client_key="198.51.100.40",
    )

    service.forgot_password(
        email="user@example.com",
        client_key="198.51.100.41",
    )
    reset_token = _token_from_url(mailer.password_reset[-1][1])
    service.reset_password(
        token=reset_token,
        new_password="new-password-12345",
        client_key="198.51.100.42",
    )

    with pytest.raises(AuthenticationFailedError):
        service.authenticate(first_session.session_token)
    with pytest.raises(TokenInvalidError):
        service.reset_password(
            token=reset_token,
            new_password="another-password-12345",
            client_key="198.51.100.42",
        )
    assert service.login(
        email="user@example.com",
        password="new-password-12345",
        client_key="198.51.100.43",
    ).user.email == "user@example.com"


def test_csrf_token_is_separate_from_session_token_and_rotates() -> None:
    service, _, mailer, _, _ = _service()
    _activate(service, mailer)
    login = service.login(
        email="user@example.com",
        password="password-12345",
        client_key="198.51.100.50",
    )

    first = service.issue_csrf(login.session_token)
    second = service.issue_csrf(login.session_token)

    assert first != login.session_token
    assert second != first
    with pytest.raises(AuthenticationFailedError):
        service.validate_csrf(login.session_token, first)
    assert (
        service.validate_csrf(login.session_token, second).user.id
        == login.user.id
    )


def test_disabling_user_atomically_revokes_all_sessions() -> None:
    service, store, mailer, _, clock = _service()
    user_id = _activate(service, mailer)
    first = service.login(
        email="user@example.com",
        password="password-12345",
        client_key="198.51.100.60",
    )
    second = service.login(
        email="user@example.com",
        password="password-12345",
        client_key="198.51.100.61",
    )

    disabled = service.disable_user(user_id)

    assert disabled.status == "disabled"
    assert disabled.disabled_at == clock.current
    assert store.get_user_by_id(user_id) == disabled
    for token in (first.session_token, second.session_token):
        with pytest.raises(AuthenticationFailedError):
            service.authenticate(token)
