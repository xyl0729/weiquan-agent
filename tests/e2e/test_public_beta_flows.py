from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import pytest
import uvicorn
from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    Playwright,
    Request,
    expect,
    sync_playwright,
)

from app.agent.errors import ProviderOutputError
from app.config import Settings
from app.main import create_app
from app.integrations.directmail import InMemoryMailSender
from tests.test_pipeline import make_pipeline


HOST = "127.0.0.1"
PORT = 8765
BASE_URL = f"http://{HOST}:{PORT}"
PASSWORD = "browser-password-12345"
NEW_PASSWORD = "browser-password-67890"
LONG_EMAIL = (
    "browser.public.beta.with.a.very.long.account.address.20260810"
    "@example.com"
)


@pytest.fixture
def public_beta_server(tmp_path: Path) -> Iterator[tuple[object, Path]]:
    pipeline, store = make_pipeline(tmp_path)
    settings = Settings(
        _env_file=None,
        deployment_mode="test",
        rollout_stage="public",
        public_base_url=BASE_URL,
        cors_origins=BASE_URL,
        llm_provider="fake",
        db_path=tmp_path / "browser-app.db",
        statutes_db_path=pipeline.settings.statutes_db_path,
        attachment_temp_dir=tmp_path / "browser-attachments",
        privacy_policy_version="2026-08-10",
    )
    pipeline.settings = settings
    application = create_app(settings, pipeline=pipeline)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=HOST,
            port=PORT,
            log_level="error",
            access_log=False,
        )
    )
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(
        target=server.run,
        name="public-beta-e2e-server",
        daemon=True,
    )
    thread.start()
    _wait_until_ready(thread)
    try:
        yield application, store.path
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            pytest.fail("E2E test server did not stop within 10 seconds")


@pytest.fixture
def browser(playwright: Playwright) -> Iterator[Browser]:
    try:
        active_browser = playwright.chromium.launch(headless=True)
    except PlaywrightError:
        active_browser = playwright.chromium.launch(
            channel="chrome",
            headless=True,
        )
    try:
        yield active_browser
    finally:
        active_browser.close()


@pytest.fixture
def playwright() -> Iterator[Playwright]:
    with sync_playwright() as active_playwright:
        yield active_playwright


def test_public_beta_browser_workflows(
    public_beta_server: tuple[object, Path],
    browser: Browser,
) -> None:
    application, database_path = public_beta_server
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    page.goto(BASE_URL, wait_until="networkidle")

    expect(page.locator("#account-summary")).to_have_text("登录 / 注册")
    expect(page.locator("#quota-summary")).to_have_text("匿名试用 5 次")
    expect(page.locator("[data-trial-only='true']").first).to_be_visible()
    expect(page.locator("[data-auth-only='true']").first).to_be_hidden()
    expect(page.locator(".trial-benefit")).to_contain_text(
        "登录后可保存历史、上传材料并使用独立额度"
    )
    page.locator("#trial-login").click()
    expect(page.locator("#auth-title")).to_have_text("登录账号")
    page.locator("#auth-close").click()
    page.locator("#trial-register").click()
    expect(page.locator("#auth-title")).to_have_text("注册公测账号")
    page.locator("#auth-close").click()
    _assert_trial_entry_viewports(page)

    trial_requests: list[dict[str, object]] = []

    def record_trial_request(request: Request) -> None:
        if urlsplit(request.url).path != "/api/trial/consult":
            return
        body = request.post_data_json
        if isinstance(body, dict):
            trial_requests.append(body)

    page.on("request", record_trial_request)
    _submit_new_trial(
        page,
        message="房东无理由扣除了两千元押金",
        first=True,
        expected_remaining=4,
    )
    expect(page.locator("#case-form")).to_be_visible()
    expect(page.locator("#case-message")).to_be_editable()
    expect(page.locator("#case-message")).to_have_attribute(
        "maxlength",
        "3000",
    )
    expect(page.locator("#case-attachment-trigger")).to_be_hidden()
    _submit_trial_followup(
        page,
        message="合同写明退租验收后七天内退押金，我已经退租十天了。",
        expected_remaining=3,
        expected_turns=2,
    )
    _submit_trial_followup(
        page,
        message="房东刚回复说墙面有划痕，要扣全部押金，我该怎么回应？",
        expected_remaining=2,
        expected_turns=3,
    )

    assert len(trial_requests) == 3
    assert "session_id" not in trial_requests[0]
    followup_session_id = trial_requests[1].get("session_id")
    assert isinstance(followup_session_id, str)
    UUID(followup_session_id)
    assert trial_requests[2].get("session_id") == followup_session_id
    assert page.evaluate(
        "() => sessionStorage.getItem('weiquan.current-session-id')"
    ) is None
    _assert_trial_case_viewport(page)

    page.reload(wait_until="networkidle")
    expect(page.locator("#new-screen")).to_be_visible()
    expect(page.locator("#case-screen")).to_be_hidden()
    expect(page.locator(".user-message")).to_have_count(0)
    expect(page.locator(".assistant-message")).to_have_count(0)
    expect(page.locator("#quota-summary")).to_have_text("试用剩余 2 次")
    assert page.evaluate(
        "() => sessionStorage.getItem('weiquan.current-session-id')"
    ) is None

    _submit_new_trial(
        page,
        message="新问题：商家发错货后拒绝退款。",
        first=False,
        expected_remaining=1,
    )
    page.locator("#trial-again").click()
    expect(page.locator("#new-screen")).to_be_visible()
    _submit_new_trial(
        page,
        message="另一个问题：培训机构拒绝退还未上课程费用。",
        first=False,
        expected_remaining=0,
    )

    expect(page.locator("#new-send")).to_be_disabled()
    exhausted = page.evaluate(
        """async () => {
          const response = await fetch("/api/trial/consult", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({message: "第六次匿名试用"}),
          });
          return {status: response.status, body: await response.json()};
        }"""
    )
    assert exhausted["status"] == 429
    assert exhausted["body"]["detail"]["code"] == "trial_quota_exceeded"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone() == (0,)

    page.locator("#account-button").click()
    page.locator("#auth-register-tab").click()
    page.locator("#auth-email").fill(LONG_EMAIL)
    page.locator("#auth-password").fill(PASSWORD)
    page.locator("#auth-privacy-accept").check()
    expect(page.locator("#captcha-slot")).to_be_hidden()
    page.evaluate(
        """() => {
          window.__weiquanTestNow = Date.now();
          Date.now = () => window.__weiquanTestNow;
        }"""
    )
    page.locator("#auth-submit").click()
    expect(page.locator("#auth-title")).to_have_text("输入邮箱验证码")

    mailer = application.state.auth_mailer
    assert isinstance(mailer, InMemoryMailSender)
    assert len(mailer.verification_messages) == 1
    expect(page.locator("#auth-resend")).to_be_disabled()
    expect(page.locator("#auth-resend")).to_contain_text("60 秒")
    page.evaluate("window.__weiquanTestNow += 61000")
    expect(page.locator("#auth-resend")).to_be_enabled()
    page.locator("#auth-resend").click()
    expect(page.locator("#auth-description")).to_contain_text(
        "新验证码已发送"
    )
    assert len(mailer.verification_messages) == 2

    verification_code = mailer.verification_messages[-1][1]
    page.locator("#auth-code").fill(verification_code)
    page.locator("#auth-submit").click()
    expect(page.locator("#auth-description")).to_have_text(
        "邮箱验证成功，请登录账号。"
    )
    page.locator("#auth-password").fill(PASSWORD)
    page.locator("#auth-submit").click()
    expect(page.locator("#account-summary")).to_have_text(LONG_EMAIL)
    expect(page.locator("#quota-summary")).to_have_text(
        "今日 10 · 本月 50"
    )

    page.locator("#new-message").fill("房东无理由扣除了两千元押金")
    page.locator("#new-send").click()
    expect(page.locator("#privacy-dialog")).to_be_visible()
    expect(page.locator("#trial-captcha-section")).to_be_hidden()
    page.locator("#privacy-accept").check()
    page.locator("#privacy-submit").click()
    expect(page.locator("#case-screen")).to_be_visible()
    expect(page.locator("#quota-summary")).to_have_text(
        "今日 9 · 本月 49"
    )
    expect(page.locator("#history-list")).to_contain_text(
        "房东无理由扣除了两千元押金"
    )

    trial_cookie = _cookie_value(context.cookies(), "weiquan_trial")
    assert trial_cookie
    page.evaluate(
        """() => {
          sessionStorage.setItem(
            "weiquan.current-session-id",
            "11111111-1111-4111-8111-111111111111",
          );
          sessionStorage.setItem(
            "weiquan.attachment-drafts",
            JSON.stringify({new: [], sessions: {}}),
          );
        }"""
    )
    page.locator("#account-button").click()
    page.locator("#auth-logout").click()
    expect(page.locator("#account-summary")).to_have_text("登录 / 注册")
    assert page.evaluate(
        """() => ({
          session: sessionStorage.getItem("weiquan.current-session-id"),
          attachments: sessionStorage.getItem("weiquan.attachment-drafts"),
        })"""
    ) == {"session": None, "attachments": None}
    assert _cookie_value(
        context.cookies(), "weiquan_trial"
    ) == trial_cookie
    assert _cookie_value(context.cookies(), "weiquan_session") is None
    expect(page.locator("#history-list")).to_be_empty()

    page.locator("#account-button").click()
    page.locator("#auth-forgot").click()
    page.locator("#auth-email").fill(LONG_EMAIL)
    page.locator("#auth-submit").click()
    expect(page.locator("#auth-description")).to_contain_text(
        "重置邮件已经发送"
    )
    assert len(mailer.password_reset_messages) == 1
    reset_url = mailer.password_reset_messages[-1][1]
    assert urlsplit(reset_url).fragment.startswith(
        "action=reset-password&token="
    )
    _open_mail_link(page, reset_url)
    expect(page).to_have_url(BASE_URL + "/")
    expect(page.locator("#auth-title")).to_have_text("设置新密码")
    page.locator("#auth-password").fill(NEW_PASSWORD)
    page.locator("#auth-submit").click()
    expect(page.locator("#auth-description")).to_have_text(
        "密码已更新，请使用新密码登录。"
    )
    page.locator("#auth-email").fill(LONG_EMAIL)
    page.locator("#auth-password").fill(NEW_PASSWORD)
    page.locator("#auth-submit").click()
    expect(page.locator("#account-summary")).to_have_text(LONG_EMAIL)

    _assert_target_viewports(page)
    context.close()


def test_trial_provider_failure_displays_contextual_safe_reply(
    public_beta_server: tuple[object, Path],
    browser: Browser,
) -> None:
    application, _ = public_beta_server
    pipeline = application.state.consultation_pipeline
    pipeline.provider._error = ProviderOutputError()  # noqa: SLF001
    context = browser.new_context(viewport={"width": 360, "height": 800})
    page = context.new_page()
    page.goto(BASE_URL, wait_until="networkidle")

    page.locator("#new-message").fill("老师打骂学生，学校一直不处理")
    page.locator("#new-send").click()
    expect(page.locator("#privacy-dialog")).to_be_visible()
    page.locator("#privacy-accept").check()
    page.locator("#privacy-submit").click()

    expect(page.locator("#case-screen")).to_be_visible()
    expect(page.locator("body")).to_contain_text(
        "孩子目前是否仍处在可能继续受到伤害的环境中？"
    )
    expect(page.locator("body")).not_to_contain_text(
        "服务返回的数据未通过完整性检查"
    )
    expect(page.locator("#quota-summary")).to_have_text("试用剩余 5 次")
    context.close()


def _submit_new_trial(
    page: Page,
    *,
    message: str,
    first: bool,
    expected_remaining: int,
) -> None:
    page.locator("#new-message").fill(message)
    page.locator("#new-send").click()
    if first:
        expect(page.locator("#privacy-dialog")).to_be_visible()
        expect(page.locator("#privacy-description")).to_have_text(
            "首次试用前需要确认当前隐私政策。"
        )
        expect(page.locator("#trial-captcha-section")).to_be_hidden()
        page.locator("#privacy-accept").check()
        page.locator("#privacy-submit").click()
    expect(page.locator("#case-screen")).to_be_visible()
    expect(page.locator("#quota-summary")).to_have_text(
        f"试用剩余 {expected_remaining} 次"
    )


def _submit_trial_followup(
    page: Page,
    *,
    message: str,
    expected_remaining: int,
    expected_turns: int,
) -> None:
    page.locator("#case-message").fill(message)
    page.locator("#case-send").click()
    expect(page.locator(".user-message")).to_have_count(expected_turns)
    expect(page.locator(".assistant-message")).to_have_count(
        expected_turns
    )
    expect(page.locator("#quota-summary")).to_have_text(
        f"试用剩余 {expected_remaining} 次"
    )


def _assert_trial_case_viewport(page: Page) -> None:
    page.set_viewport_size({"width": 360, "height": 800})
    expect(page.locator("#case-screen")).to_be_visible()
    expect(page.locator("#case-form")).to_be_visible()
    expect(page.locator("#case-message")).to_be_editable()
    expect(page.locator("#case-send")).to_be_visible()
    expect(page.locator("#case-attachment-trigger")).to_be_hidden()
    page.locator("#case-form").scroll_into_view_if_needed()
    measurements = page.evaluate(
        """() => {
          const form = document.querySelector("#case-form");
          const message = document.querySelector("#case-message");
          const send = document.querySelector("#case-send");
          const formRect = form.getBoundingClientRect();
          const messageRect = message.getBoundingClientRect();
          const sendRect = send.getBoundingClientRect();
          const centerOwner = (element, rect) => {
            const owner = document.elementFromPoint(
              rect.left + rect.width / 2,
              rect.top + rect.height / 2,
            );
            return owner === element || element.contains(owner);
          };
          return {
            documentOverflow:
              document.documentElement.scrollWidth >
              document.documentElement.clientWidth,
            bodyOverflow:
              document.body.scrollWidth > document.body.clientWidth,
            formInside:
              formRect.left >= 0 &&
              formRect.right <= window.innerWidth,
            messageInside:
              messageRect.left >= formRect.left &&
              messageRect.right <= formRect.right,
            sendInside:
              sendRect.left >= formRect.left &&
              sendRect.right <= formRect.right,
            messageUncovered: centerOwner(message, messageRect),
            sendUncovered: centerOwner(send, sendRect),
          };
        }"""
    )
    assert measurements == {
        "documentOverflow": False,
        "bodyOverflow": False,
        "formInside": True,
        "messageInside": True,
        "sendInside": True,
        "messageUncovered": True,
        "sendUncovered": True,
    }
    page.set_viewport_size({"width": 1366, "height": 768})


def _assert_target_viewports(page: Page) -> None:
    for width, height in (
        (1366, 768),
        (936, 900),
        (390, 844),
        (360, 800),
    ):
        page.set_viewport_size({"width": width, "height": height})
        page.locator("#account-button").click()
        page.locator("#auth-logout").scroll_into_view_if_needed()
        expect(page.locator("#auth-logout")).to_be_visible()
        measurements = page.evaluate(
            """() => {
              const dialog = document.querySelector("#auth-dialog");
              const summary = document.querySelector("#account-summary");
              const rect = dialog.getBoundingClientRect();
              const summaryRect = summary.getBoundingClientRect();
              return {
                documentOverflow:
                  document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
                bodyOverflow: document.body.scrollWidth >
                  document.body.clientWidth,
                dialogLeft: rect.left,
                dialogRight: rect.right,
                viewportWidth: window.innerWidth,
                summaryInside:
                  summaryRect.left >= 0 &&
                  summaryRect.right <= window.innerWidth,
              };
            }"""
        )
        assert measurements["documentOverflow"] is False
        assert measurements["bodyOverflow"] is False
        assert measurements["dialogLeft"] >= 0
        assert measurements["dialogRight"] <= measurements["viewportWidth"]
        assert measurements["summaryInside"] is True
        page.locator("#auth-close").click()


def _assert_trial_entry_viewports(page: Page) -> None:
    for width, height in (
        (1366, 768),
        (390, 844),
        (360, 800),
    ):
        page.set_viewport_size({"width": width, "height": height})
        expect(page.locator(".trial-benefit")).to_be_visible()
        expect(page.locator("#trial-login")).to_be_visible()
        expect(page.locator("#trial-register")).to_be_visible()
        measurements = page.evaluate(
            """() => {
              const strip = document.querySelector(".trial-strip");
              const login = document.querySelector("#trial-login");
              const register = document.querySelector("#trial-register");
              const stripRect = strip.getBoundingClientRect();
              const loginRect = login.getBoundingClientRect();
              const registerRect = register.getBoundingClientRect();
              const overlaps = !(
                loginRect.right <= registerRect.left ||
                registerRect.right <= loginRect.left ||
                loginRect.bottom <= registerRect.top ||
                registerRect.bottom <= loginRect.top
              );
              return {
                documentOverflow:
                  document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
                bodyOverflow:
                  document.body.scrollWidth > document.body.clientWidth,
                stripInside:
                  stripRect.left >= 0 &&
                  stripRect.right <= window.innerWidth,
                loginInside:
                  loginRect.left >= stripRect.left &&
                  loginRect.right <= stripRect.right,
                registerInside:
                  registerRect.left >= stripRect.left &&
                  registerRect.right <= stripRect.right,
                buttonsOverlap: overlaps,
              };
            }"""
        )
        assert measurements["documentOverflow"] is False
        assert measurements["bodyOverflow"] is False
        assert measurements["stripInside"] is True
        assert measurements["loginInside"] is True
        assert measurements["registerInside"] is True
        assert measurements["buttonsOverlap"] is False
    page.set_viewport_size({"width": 1366, "height": 768})


def _open_mail_link(page: Page, url: str) -> None:
    page.goto("about:blank")
    page.goto(url, wait_until="networkidle")


def _cookie_value(cookies: list[dict[str, object]], name: str) -> str | None:
    for cookie in cookies:
        if cookie.get("name") == name:
            value = cookie.get("value")
            return value if isinstance(value, str) else None
    return None


def _wait_until_ready(thread: threading.Thread) -> None:
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if not thread.is_alive():
            pytest.fail("E2E test server exited before becoming ready")
        try:
            response = httpx.get(f"{BASE_URL}/health", timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.05)
    pytest.fail(f"E2E test server did not become ready: {last_error}")
