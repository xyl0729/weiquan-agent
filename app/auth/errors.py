from __future__ import annotations

from app.agent.errors import SafeApplicationError


class AuthError(SafeApplicationError):
    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        status_code: int,
    ) -> None:
        super().__init__(code, safe_message)
        self.status_code = status_code


class AuthenticationFailedError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "invalid_credentials",
            "邮箱或密码错误",
            status_code=401,
        )


class RegistrationRequiredError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "registration_required",
            "请先注册或登录后使用此功能",
            status_code=401,
        )


class CsrfInvalidError(AuthenticationFailedError):
    def __init__(self) -> None:
        AuthError.__init__(
            self,
            "csrf_invalid",
            "请求安全校验失败，请刷新页面后重试",
            status_code=403,
        )


class SameOriginRequiredError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "same_origin_required",
            "请求来源校验失败",
            status_code=403,
        )


class RegistrationCapacityError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "registration_capacity_full",
            "公测名额已满",
            status_code=409,
        )


class RegistrationClosedError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "public_registration_closed",
            "当前仅开放受控账号，暂未开放公开注册",
            status_code=403,
        )


class CaptchaFailedError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "captcha_failed",
            "验证码校验失败，请重试",
            status_code=422,
        )


class MailUnavailableError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "mail_unavailable",
            "验证邮件暂时无法发送，请稍后重试",
            status_code=503,
        )


class TokenInvalidError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "token_invalid",
            "链接无效或已过期",
            status_code=422,
        )


class VerificationCodeInvalidError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "verification_code_invalid",
            "验证码无效或已过期",
            status_code=422,
        )


class PasswordInvalidError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "password_invalid",
            "密码长度必须为 8 至 128 个字符",
            status_code=422,
        )


class EmailInvalidError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "email_invalid",
            "邮箱格式无效",
            status_code=422,
        )


class PrivacyRequiredError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "privacy_acceptance_required",
            "请先确认当前隐私政策",
            status_code=409,
        )


class AuthRateLimitError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "auth_rate_limited",
            "操作过于频繁，请稍后重试",
            status_code=429,
        )
