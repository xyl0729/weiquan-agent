from __future__ import annotations

import pytest
from alibabacloud_captcha20230305 import models as captcha_models

from app.integrations.captcha import (
    AliyunCaptchaVerifier,
    CaptchaVerificationError,
    DisabledCaptchaVerifier,
)
from app.integrations.directmail import (
    AliyunDirectMailSender,
    MailDeliveryError,
)


class DirectMailClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[object] = []

    def single_send_mail(self, request: object) -> object:
        if self.error is not None:
            raise self.error
        self.requests.append(request)
        return object()


class CaptchaClient:
    def __init__(
        self,
        *,
        verified: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.verified = verified
        self.error = error
        self.requests: list[object] = []

    def verify_captcha(self, request: object) -> object:
        if self.error is not None:
            raise self.error
        self.requests.append(request)
        return captcha_models.VerifyCaptchaResponse(
            body=captcha_models.VerifyCaptchaResponseBody(
                success=True,
                result=captcha_models.VerifyCaptchaResponseBodyResult(
                    verify_result=self.verified,
                ),
            )
        )


def test_disabled_captcha_accepts_missing_token_without_a_client() -> None:
    verifier = DisabledCaptchaVerifier()

    assert verifier.verify(token="", remote_ip="198.51.100.1") is True


def test_directmail_adapter_builds_safe_verification_and_reset_messages() -> None:
    client = DirectMailClient()
    sender = AliyunDirectMailSender(
        client=client,
        account_name="notice@example.test",
        from_alias="维权咨询助手",
    )

    sender.send_verification(
        to_email="user@example.test",
        verification_code="012345",
    )
    sender.send_password_reset(
        to_email="user@example.test",
        reset_url="https://weiquan.example.test/#token=reset-secret",
    )

    verification, reset = client.requests
    assert verification.account_name == "notice@example.test"
    assert verification.to_address == "user@example.test"
    assert verification.reply_to_address is False
    assert "验证" in verification.subject
    assert "012345" in verification.html_body
    assert "10 分钟" in verification.html_body
    assert "href=" not in verification.html_body
    assert "重置" in reset.subject
    assert "reset-secret" in reset.html_body


def test_directmail_adapter_maps_sdk_errors_without_exposing_details() -> None:
    outcomes: list[str] = []
    sender = AliyunDirectMailSender(
        client=DirectMailClient(error=RuntimeError("sdk secret detail")),
        account_name="notice@example.test",
        from_alias="维权咨询助手",
        outcome_recorder=outcomes.append,
    )

    with pytest.raises(MailDeliveryError) as caught:
        sender.send_verification(
            to_email="user@example.test",
            verification_code="123456",
        )

    assert "sdk secret detail" not in str(caught.value)
    assert "secret" not in str(caught.value).casefold()
    assert outcomes == ["failure"]


def test_captcha_adapter_returns_verified_result_and_maps_sdk_errors() -> None:
    outcomes: list[str] = []
    accepted_client = CaptchaClient(verified=True)
    rejected_client = CaptchaClient(verified=False)
    accepted = AliyunCaptchaVerifier(
        client=accepted_client,
        scene_id="scene-id",
        outcome_recorder=outcomes.append,
    )
    rejected = AliyunCaptchaVerifier(
        client=rejected_client,
        scene_id="scene-id",
        outcome_recorder=outcomes.append,
    )

    assert accepted.verify(token="captcha-param", remote_ip="198.51.100.1")
    assert not rejected.verify(
        token="captcha-param",
        remote_ip="198.51.100.1",
    )
    assert (
        accepted_client.requests[0].captcha_verify_param
        == "captcha-param"
    )

    failing = AliyunCaptchaVerifier(
        client=CaptchaClient(error=RuntimeError("sdk secret detail")),
        scene_id="scene-id",
        outcome_recorder=outcomes.append,
    )
    with pytest.raises(CaptchaVerificationError) as caught:
        failing.verify(token="captcha-param", remote_ip="198.51.100.1")
    assert "sdk secret detail" not in str(caught.value)
    assert outcomes == ["success", "rejected", "failure"]
