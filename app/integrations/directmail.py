from __future__ import annotations

from html import escape
from collections.abc import Callable
from typing import Any, Protocol

from alibabacloud_dm20151123 import models as dm_models
from alibabacloud_dm20151123.client import Client as DirectMailClient
from alibabacloud_tea_openapi import models as open_api_models


class MailDeliveryError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("邮件服务暂时不可用")


class MailSender(Protocol):
    def send_verification(
        self,
        *,
        to_email: str,
        verification_code: str,
    ) -> None: ...

    def send_password_reset(
        self,
        *,
        to_email: str,
        reset_url: str,
    ) -> None: ...


class InMemoryMailSender:
    def __init__(self) -> None:
        self.verification_messages: list[tuple[str, str]] = []
        self.password_reset_messages: list[tuple[str, str]] = []

    def send_verification(
        self,
        *,
        to_email: str,
        verification_code: str,
    ) -> None:
        self.verification_messages.append((to_email, verification_code))

    def send_password_reset(
        self,
        *,
        to_email: str,
        reset_url: str,
    ) -> None:
        self.password_reset_messages.append((to_email, reset_url))


class AliyunDirectMailSender:
    def __init__(
        self,
        *,
        client: Any,
        account_name: str,
        from_alias: str,
        outcome_recorder: Callable[[str], None] | None = None,
    ) -> None:
        if not account_name.strip():
            raise ValueError("DirectMail 发信地址不能为空")
        self._client = client
        self._account_name = account_name.strip()
        self._from_alias = from_alias.strip()
        self._outcome_recorder = outcome_recorder

    @classmethod
    def from_credentials(
        cls,
        *,
        access_key_id: str,
        access_key_secret: str,
        account_name: str,
        from_alias: str,
        region: str,
        outcome_recorder: Callable[[str], None] | None = None,
    ) -> "AliyunDirectMailSender":
        endpoint = (
            "dm.aliyuncs.com"
            if region == "cn-hangzhou"
            else f"dm.{region}.aliyuncs.com"
        )
        config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            region_id=region,
            endpoint=endpoint,
            connect_timeout=5000,
            read_timeout=10000,
        )
        return cls(
            client=DirectMailClient(config),
            account_name=account_name,
            from_alias=from_alias,
            outcome_recorder=outcome_recorder,
        )

    def send_verification(
        self,
        *,
        to_email: str,
        verification_code: str,
    ) -> None:
        safe_code = escape(verification_code)
        request = dm_models.SingleSendMailRequest(
            account_name=self._account_name,
            address_type=1,
            reply_to_address=False,
            from_alias=self._from_alias,
            to_address=to_email,
            subject="维权咨询助手邮箱验证码",
            click_trace="0",
            text_body=(
                f"你的邮箱验证码是：{verification_code}\n"
                "验证码 10 分钟内有效，请勿转发给他人。"
            ),
            html_body=(
                "<p>您好：</p>"
                "<p>你的邮箱验证码是：</p>"
                '<p style="font-size:24px;font-weight:700;'
                f'letter-spacing:4px">{safe_code}</p>'
                "<p>验证码 10 分钟内有效，请勿转发给他人。</p>"
                "<p>如非本人操作，请忽略本邮件。</p>"
            ),
        )
        self._deliver(request)

    def send_password_reset(
        self,
        *,
        to_email: str,
        reset_url: str,
    ) -> None:
        self._send(
            to_email=to_email,
            subject="重置你的维权咨询助手密码",
            action="请在 30 分钟内重置密码",
            url=reset_url,
        )

    def _send(
        self,
        *,
        to_email: str,
        subject: str,
        action: str,
        url: str,
    ) -> None:
        safe_url = escape(url, quote=True)
        request = dm_models.SingleSendMailRequest(
            account_name=self._account_name,
            address_type=1,
            reply_to_address=False,
            from_alias=self._from_alias,
            to_address=to_email,
            subject=subject,
            click_trace="0",
            text_body=f"{action}：{url}",
            html_body=(
                "<p>您好：</p>"
                f"<p>{escape(action)}。</p>"
                f'<p><a href="{safe_url}">打开安全链接</a></p>'
                "<p>如非本人操作，请忽略本邮件。</p>"
            ),
        )
        self._deliver(request)

    def _deliver(self, request: object) -> None:
        try:
            self._client.single_send_mail(request)
        except Exception as exc:
            self._record("failure")
            raise MailDeliveryError() from exc
        self._record("success")

    def _record(self, outcome: str) -> None:
        if self._outcome_recorder is None:
            return
        try:
            self._outcome_recorder(outcome)
        except Exception:
            return
