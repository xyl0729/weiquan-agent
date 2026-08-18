from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

from email_validator import EmailNotValidError, validate_email

from app.auth.errors import (
    AuthenticationFailedError,
    AuthRateLimitError,
    CaptchaFailedError,
    CsrfInvalidError,
    EmailInvalidError,
    MailUnavailableError,
    PasswordInvalidError,
    PrivacyRequiredError,
    RegistrationCapacityError,
    RegistrationClosedError,
    TokenInvalidError,
    VerificationCodeInvalidError,
)
from app.auth.models import (
    AuthContext,
    LoginResult,
    PrivacyContext,
    RegistrationResult,
    UserRecord,
)
from app.auth.passwords import PasswordManager, PasswordPolicyError
from app.auth.store import AuthStore
from app.auth.tokens import (
    issue_opaque_token,
    issue_verification_code,
    token_digest,
    verification_code_digest,
)
from app.integrations.captcha import CaptchaVerifier
from app.integrations.directmail import MailSender
from app.privacy.policy import PrivacyPolicy


DEFAULT_RATE_LIMITS: dict[str, int] = {
    "register": 10,
    "resend_verification": 5,
    "verify_email": 20,
    "login": 20,
    "forgot_password": 5,
    "reset_password": 10,
}


def normalize_email(value: str) -> str:
    candidate = value.strip()
    try:
        normalized = validate_email(
            candidate,
            check_deliverability=False,
        ).normalized
    except (EmailNotValidError, ValueError) as exc:
        raise EmailInvalidError() from exc
    normalized = normalized.casefold()
    if len(normalized) > 320:
        raise EmailInvalidError()
    return normalized


class AuthService:
    def __init__(
        self,
        *,
        store: AuthStore,
        passwords: PasswordManager,
        mailer: MailSender,
        captcha: CaptchaVerifier,
        policy: PrivacyPolicy,
        public_base_url: str,
        rate_limit_secret: bytes,
        verification_code_secret: bytes | None = None,
        now: Callable[[], datetime] | None = None,
        session_ttl: timedelta = timedelta(days=7),
        token_ttl: timedelta = timedelta(minutes=30),
        verification_code_ttl: timedelta = timedelta(minutes=10),
        pending_ttl: timedelta = timedelta(hours=24),
        rate_limit_window: timedelta = timedelta(hours=1),
        rate_limits: Mapping[str, int] | None = None,
        rollout_stage: Literal[
            "internal",
            "invited",
            "public",
        ] = "public",
        invited_user_limit: int = 10,
    ) -> None:
        normalized_base_url = public_base_url.strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("公开站点地址不能为空")
        if len(rate_limit_secret) < 32:
            raise ValueError("认证限流密钥不能短于 32 字节")
        if verification_code_secret is None:
            verification_code_secret = hmac.new(
                rate_limit_secret,
                b"weiquan-email-verification-code-v1",
                hashlib.sha256,
            ).digest()
        if len(verification_code_secret) < 32:
            raise ValueError("邮箱验证码密钥不能短于 32 字节")
        if rollout_stage not in {"internal", "invited", "public"}:
            raise ValueError("发布阶段无效")
        if not 1 <= invited_user_limit <= 10:
            raise ValueError("观察期账号上限必须在 1 至 10 之间")
        for name, duration in {
            "session_ttl": session_ttl,
            "token_ttl": token_ttl,
            "verification_code_ttl": verification_code_ttl,
            "pending_ttl": pending_ttl,
            "rate_limit_window": rate_limit_window,
        }.items():
            if duration <= timedelta(0):
                raise ValueError(f"{name} 必须大于 0")

        self.store = store
        self.passwords = passwords
        self.mailer = mailer
        self.captcha = captcha
        self.policy = policy
        self.public_base_url = normalized_base_url
        self._rate_limit_secret = bytes(rate_limit_secret)
        self._verification_code_secret = bytes(
            verification_code_secret
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._session_ttl = session_ttl
        self._token_ttl = token_ttl
        self._verification_code_ttl = verification_code_ttl
        self._pending_ttl = pending_ttl
        self._rate_limit_window = rate_limit_window
        self._rate_limits = dict(
            rate_limits if rate_limits is not None else DEFAULT_RATE_LIMITS
        )
        self.rollout_stage = rollout_stage
        self.invited_user_limit = invited_user_limit

    def register(
        self,
        *,
        email: str,
        password: str,
        captcha_token: str,
        privacy_version: str,
        privacy_accepted: bool,
        client_key: str,
    ) -> RegistrationResult:
        if self.rollout_stage != "public":
            raise RegistrationClosedError()
        now = self._clock()
        self._consume_rate_limit(
            action="register",
            client_key=client_key,
            now=now,
        )
        if (
            not privacy_accepted
            or privacy_version.strip() != self.policy.version
        ):
            raise PrivacyRequiredError()
        self._verify_captcha(
            token=captcha_token,
            client_key=client_key,
        )
        normalized_email = normalize_email(email)
        password_hash = self._hash_password(password)
        decision = self.store.register_pending(
            email=normalized_email,
            password_hash=password_hash,
            policy_version=self.policy.version,
            now=now,
            pending_ttl=self._pending_ttl,
        )
        if decision.status == "capacity_full":
            raise RegistrationCapacityError()
        if decision.user is None:
            raise RuntimeError("认证存储返回了不完整的注册结果")
        if decision.status == "duplicate":
            email_sent = decision.user.status == "pending_verification"
            if email_sent:
                self._send_verification(user=decision.user, now=now)
            return RegistrationResult(
                user=decision.user,
                email_sent=email_sent,
                created=False,
            )

        self._send_verification(user=decision.user, now=now)
        return RegistrationResult(
            user=decision.user,
            email_sent=True,
            created=True,
        )

    def resend_verification(
        self,
        *,
        email: str,
        client_key: str,
    ) -> None:
        now = self._clock()
        self._consume_rate_limit(
            action="resend_verification",
            client_key=client_key,
            now=now,
        )
        try:
            normalized_email = normalize_email(email)
        except EmailInvalidError:
            return
        user = self.store.get_user_by_email(normalized_email)
        if user is None or user.status != "pending_verification":
            return
        self._send_verification(user=user, now=now)

    def invite_user(
        self,
        *,
        email: str,
        password: str,
        privacy_version: str,
        privacy_accepted: bool,
    ) -> RegistrationResult:
        now = self._clock()
        self._validate_privacy_acceptance(
            privacy_version=privacy_version,
            privacy_accepted=privacy_accepted,
        )
        decision = self.store.register_pending(
            email=normalize_email(email),
            password_hash=self._hash_password(password),
            policy_version=self.policy.version,
            now=now,
            pending_ttl=self._pending_ttl,
            capacity_limit=(
                self.invited_user_limit
                if self.rollout_stage == "invited"
                else None
            ),
        )
        if decision.status == "capacity_full":
            raise RegistrationCapacityError()
        if decision.user is None:
            raise RuntimeError("认证存储返回了不完整的邀请结果")
        if decision.status == "duplicate":
            email_sent = decision.user.status == "pending_verification"
            if email_sent:
                self._send_verification(user=decision.user, now=now)
            return RegistrationResult(
                user=decision.user,
                email_sent=email_sent,
                created=False,
            )
        self._send_verification(user=decision.user, now=now)
        return RegistrationResult(
            user=decision.user,
            email_sent=True,
            created=True,
        )

    def create_admin_account(
        self,
        *,
        email: str,
        password: str,
        privacy_version: str,
        privacy_accepted: bool,
    ) -> UserRecord:
        now = self._clock()
        self._validate_privacy_acceptance(
            privacy_version=privacy_version,
            privacy_accepted=privacy_accepted,
        )
        decision = self.store.create_verified_user(
            email=normalize_email(email),
            password_hash=self._hash_password(password),
            role="admin",
            policy_version=self.policy.version,
            now=now,
            pending_ttl=self._pending_ttl,
        )
        if decision.status == "capacity_full":
            raise RegistrationCapacityError()
        if decision.status == "duplicate":
            raise ValueError("账号已存在")
        if decision.user is None:
            raise RuntimeError("认证存储返回了不完整的管理员创建结果")
        return decision.user

    def verify_email(
        self,
        *,
        email: str,
        code: str,
        client_key: str,
    ) -> UserRecord:
        now = self._clock()
        self._consume_rate_limit(
            action="verify_email",
            client_key=client_key,
            now=now,
        )
        try:
            normalized_email = normalize_email(email)
        except EmailInvalidError as exc:
            raise VerificationCodeInvalidError() from exc
        normalized_code = code.strip()
        if len(normalized_code) != 6 or not normalized_code.isascii():
            raise VerificationCodeInvalidError()
        if not normalized_code.isdigit():
            raise VerificationCodeInvalidError()
        digest = verification_code_digest(
            email=normalized_email,
            code=normalized_code,
            secret=self._verification_code_secret,
        )
        user = self.store.consume_email_verification(
            token_digest=digest,
            now=now,
        )
        if user is None:
            raise VerificationCodeInvalidError()
        return user

    def login(
        self,
        *,
        email: str,
        password: str,
        client_key: str,
    ) -> LoginResult:
        now = self._clock()
        self._consume_rate_limit(
            action="login",
            client_key=client_key,
            now=now,
        )
        try:
            normalized_email = normalize_email(email)
        except EmailInvalidError as exc:
            raise AuthenticationFailedError() from exc
        user = self.store.get_user_by_email(normalized_email)
        if user is None or user.status != "active":
            raise AuthenticationFailedError()
        verification = self.passwords.verify(
            password,
            user.password_hash,
        )
        if not verification.valid:
            raise AuthenticationFailedError()
        if verification.upgraded_hash is not None:
            self.store.update_password_hash(
                user_id=user.id,
                password_hash=verification.upgraded_hash,
                now=now,
            )

        issued = issue_opaque_token()
        expires_at = now + self._session_ttl
        self.store.create_session(
            user_id=user.id,
            token_digest=issued.digest,
            now=now,
            expires_at=expires_at,
        )
        return LoginResult(
            user=user,
            session_token=issued.value,
            expires_at=expires_at,
        )

    def authenticate(self, session_token: str) -> AuthContext:
        value = session_token.strip()
        if not value:
            raise AuthenticationFailedError()
        context = self.store.get_auth_context(
            token_digest=token_digest(value),
            now=self._clock(),
        )
        if context is None:
            raise AuthenticationFailedError()
        return context

    def logout(self, session_token: str) -> None:
        value = session_token.strip()
        if not value:
            return
        self.store.revoke_session(
            token_digest=token_digest(value),
            now=self._clock(),
        )

    def disable_user(self, user_id: str) -> UserRecord:
        user = self.store.disable_user(
            user_id=user_id,
            now=self._clock(),
        )
        if user is None:
            raise AuthenticationFailedError()
        return user

    def forgot_password(
        self,
        *,
        email: str,
        client_key: str,
    ) -> None:
        now = self._clock()
        self._consume_rate_limit(
            action="forgot_password",
            client_key=client_key,
            now=now,
        )
        try:
            normalized_email = normalize_email(email)
        except EmailInvalidError:
            return
        user = self.store.get_user_by_email(normalized_email)
        if user is None or user.status != "active":
            return

        issued = issue_opaque_token()
        self.store.replace_token(
            user_id=user.id,
            purpose="password_reset",
            token_digest=issued.digest,
            now=now,
            expires_at=now + self._token_ttl,
        )
        reset_url = self._fragment_url(
            action="reset-password",
            token=issued.value,
        )
        try:
            self.mailer.send_password_reset(
                to_email=user.email,
                reset_url=reset_url,
            )
        except Exception as exc:
            raise MailUnavailableError() from exc

    def reset_password(
        self,
        *,
        token: str,
        new_password: str,
        client_key: str,
    ) -> UserRecord:
        now = self._clock()
        self._consume_rate_limit(
            action="reset_password",
            client_key=client_key,
            now=now,
        )
        password_hash = self._hash_password(new_password)
        user = self.store.consume_password_reset(
            token_digest=token_digest(token.strip()),
            new_password_hash=password_hash,
            now=now,
        )
        if user is None:
            raise TokenInvalidError()
        return user

    def issue_csrf(self, session_token: str) -> str:
        self.authenticate(session_token)
        issued = issue_opaque_token()
        stored = self.store.set_csrf_digest(
            token_digest=token_digest(session_token.strip()),
            csrf_digest=issued.digest,
            now=self._clock(),
        )
        if not stored:
            raise AuthenticationFailedError()
        return issued.value

    def validate_csrf(
        self,
        session_token: str,
        csrf_token: str,
    ) -> AuthContext:
        session_value = session_token.strip()
        csrf_value = csrf_token.strip()
        if not session_value or not csrf_value:
            raise CsrfInvalidError()
        context = self.store.validate_csrf_digest(
            token_digest=token_digest(session_value),
            csrf_digest=token_digest(csrf_value),
            now=self._clock(),
        )
        if context is None:
            raise CsrfInvalidError()
        return context

    def accept_privacy(
        self,
        *,
        user_id: str,
        context: PrivacyContext,
        policy_version: str,
    ) -> None:
        if policy_version.strip() != self.policy.version:
            raise PrivacyRequiredError()
        if self.store.get_user_by_id(user_id) is None:
            raise AuthenticationFailedError()
        self.store.record_privacy_acceptance(
            user_id=user_id,
            context=context,
            policy_version=self.policy.version,
            accepted_at=self._clock(),
        )

    def requires_privacy_acceptance(
        self,
        *,
        user_id: str,
        context: PrivacyContext,
    ) -> bool:
        return not self.store.has_privacy_acceptance(
            user_id=user_id,
            context=context,
            policy_version=self.policy.version,
        )

    def _send_verification(
        self,
        *,
        user: UserRecord,
        now: datetime,
    ) -> None:
        issued = issue_verification_code(
            email=user.email,
            secret=self._verification_code_secret,
        )
        self.store.replace_token(
            user_id=user.id,
            purpose="email_verification",
            token_digest=issued.digest,
            now=now,
            expires_at=now + self._verification_code_ttl,
        )
        try:
            self.mailer.send_verification(
                to_email=user.email,
                verification_code=issued.value,
            )
        except Exception as exc:
            raise MailUnavailableError() from exc

    def _verify_captcha(self, *, token: str, client_key: str) -> None:
        try:
            verified = self.captcha.verify(
                token=token,
                remote_ip=client_key,
            )
        except Exception as exc:
            raise CaptchaFailedError() from exc
        if not verified:
            raise CaptchaFailedError()

    def _validate_privacy_acceptance(
        self,
        *,
        privacy_version: str,
        privacy_accepted: bool,
    ) -> None:
        if (
            not privacy_accepted
            or privacy_version.strip() != self.policy.version
        ):
            raise PrivacyRequiredError()

    def _hash_password(self, password: str) -> str:
        try:
            return self.passwords.hash(password)
        except PasswordPolicyError as exc:
            raise PasswordInvalidError() from exc

    def _consume_rate_limit(
        self,
        *,
        action: str,
        client_key: str,
        now: datetime,
    ) -> None:
        limit = self._rate_limits.get(action)
        if limit is None:
            return
        if limit < 1:
            raise AuthRateLimitError()
        window_seconds = int(self._rate_limit_window.total_seconds())
        timestamp = int(now.timestamp())
        window_started_at = datetime.fromtimestamp(
            timestamp - (timestamp % window_seconds),
            tz=UTC,
        )
        key_digest = hmac.new(
            self._rate_limit_secret,
            f"{action}\0{client_key}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        accepted = self.store.consume_rate_limit(
            action=action,
            key_digest=key_digest,
            window_started_at=window_started_at,
            expires_at=window_started_at + self._rate_limit_window,
            limit=limit,
        )
        if not accepted:
            raise AuthRateLimitError()

    def _fragment_url(self, *, action: str, token: str) -> str:
        fragment = urlencode({"action": action, "token": token})
        return f"{self.public_base_url}/#{fragment}"

    def _clock(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("认证时钟必须返回带时区时间")
        return value.astimezone(UTC)
