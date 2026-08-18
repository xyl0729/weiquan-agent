from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from alibabacloud_captcha20230305 import models as captcha_models
from alibabacloud_captcha20230305.client import Client as CaptchaClient
from alibabacloud_tea_openapi import models as open_api_models


class CaptchaVerificationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("验证码服务暂时不可用")


class CaptchaVerifier(Protocol):
    def verify(self, *, token: str, remote_ip: str) -> bool: ...


class DisabledCaptchaVerifier:
    def verify(self, *, token: str, remote_ip: str) -> bool:
        del token, remote_ip
        return True


class DevelopmentCaptchaVerifier:
    def verify(self, *, token: str, remote_ip: str) -> bool:
        del remote_ip
        return bool(token.strip())


class AliyunCaptchaVerifier:
    def __init__(
        self,
        *,
        client: Any,
        scene_id: str,
        outcome_recorder: Callable[[str], None] | None = None,
    ) -> None:
        if not scene_id.strip():
            raise ValueError("CAPTCHA 场景 ID 不能为空")
        self._client = client
        self._scene_id = scene_id.strip()
        self._outcome_recorder = outcome_recorder

    @classmethod
    def from_credentials(
        cls,
        *,
        access_key_id: str,
        access_key_secret: str,
        scene_id: str,
        endpoint: str,
        outcome_recorder: Callable[[str], None] | None = None,
    ) -> "AliyunCaptchaVerifier":
        config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=endpoint,
            connect_timeout=5000,
            read_timeout=10000,
        )
        return cls(
            client=CaptchaClient(config),
            scene_id=scene_id,
            outcome_recorder=outcome_recorder,
        )

    def verify(self, *, token: str, remote_ip: str) -> bool:
        del remote_ip
        if not token.strip():
            self._record("rejected")
            return False
        request = captcha_models.VerifyCaptchaRequest(
            captcha_verify_param=token,
        )
        try:
            response = self._client.verify_captcha(request)
            body = response.body
            result = body.result if body is not None else None
            verified = bool(
                body is not None
                and body.success
                and result is not None
                and result.verify_result
            )
            self._record("success" if verified else "rejected")
            return verified
        except Exception as exc:
            self._record("failure")
            raise CaptchaVerificationError() from exc

    def _record(self, outcome: str) -> None:
        if self._outcome_recorder is None:
            return
        try:
            self._outcome_recorder(outcome)
        except Exception:
            return
