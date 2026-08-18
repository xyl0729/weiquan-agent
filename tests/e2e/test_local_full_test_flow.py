from __future__ import annotations

from copy import deepcopy
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import uvicorn
from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    Playwright,
    expect,
    sync_playwright,
)

from app.attachments.service import AttachmentService
from app.config import Settings
from app.main import create_app
from tests.test_api import ApiExtractionWorker
from tests.test_pipeline import make_pipeline


HOST = "127.0.0.1"
PORT = 8766
BASE_URL = f"http://{HOST}:{PORT}"
FIRST_MESSAGE = "房东不退押金"
FOLLOWUP_MESSAGE = (
    "押金2000元，房东扣2000元，没理由，而且合同没写可以扣。"
)
LEGACY_UNVERIFIED_LIMITATION = (
    "该主题尚未经过本项目的本地法条与确定性规则核验。"
)
GENERAL_GUIDANCE_LIMITATION = (
    "当前提供的是该类问题的一般处理建议，具体责任和适用规则"
    "仍需结合事实与证据进一步核对。"
)
ATTACHMENT_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "attachments"
    / "selectable.pdf"
)


@pytest.fixture
def local_full_test_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[object]:
    pipeline, store = make_pipeline(tmp_path)
    settings = Settings(
        _env_file=None,
        deployment_mode="local",
        public_base_url=BASE_URL,
        cors_origins=BASE_URL,
        llm_provider="fake",
        db_path=store.path,
        statutes_db_path=pipeline.settings.statutes_db_path,
        attachment_temp_dir=tmp_path / "browser-attachments",
    )
    pipeline.settings = settings
    service = AttachmentService(
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
        worker=ApiExtractionWorker(),
    )
    application = create_app(settings, pipeline=pipeline)
    application.state.attachment_store = pipeline.attachments
    application.state.attachment_service = service
    monkeypatch.setattr("app.main.probe_ocr_readiness", lambda: True)

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
        name="local-full-test-e2e-server",
        daemon=True,
    )
    thread.start()
    _wait_until_ready(thread)
    try:
        yield application
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            pytest.fail("E2E test server did not stop within 10 seconds")


@pytest.fixture
def playwright() -> Iterator[Playwright]:
    with sync_playwright() as active_playwright:
        yield active_playwright


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


def test_local_full_test_browser_workflow(
    local_full_test_server: object,
    browser: Browser,
) -> None:
    del local_full_test_server
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    requested_paths: list[str] = []
    page.on(
        "request",
        lambda request: requested_paths.append(
            urlsplit(request.url).path
        ),
    )

    page.goto(BASE_URL, wait_until="networkidle")

    expect(page.locator("#account-summary")).to_have_text(
        "本地完整测试"
    )
    expect(page.locator("#quota-summary")).to_have_text(
        "不计应用额度"
    )
    expect(page.locator("#quota-summary")).to_have_attribute(
        "title",
        "真实模型调用仍可能产生 API 费用。",
    )
    expect(page.locator("#account-button")).to_be_disabled()
    expect(page.locator(".trial-strip")).to_be_hidden()
    expect(page.locator("#history-panel")).to_be_visible()
    expect(page.locator("#new-attachment-trigger")).to_be_visible()
    assert "/api/runtime-config" in requested_paths
    _assert_no_account_bootstrap_requests(requested_paths)

    page.locator("#new-message").fill(FIRST_MESSAGE)
    with page.expect_request(_is_consult_request) as first_request_info:
        with page.expect_response(_is_consult_response) as first_response_info:
            page.locator("#new-send").click()
    first_request = first_request_info.value
    first_response = first_response_info.value
    first_request_body = first_request.post_data_json
    first_response_body = first_response.json()
    session_id = first_response_body["session_id"]

    assert first_response.status == 200
    assert first_request_body == {
        "message": FIRST_MESSAGE,
        "attachment_ids": [],
    }
    expect(page.locator("#case-screen")).to_be_visible()
    expect(page.locator("#thread")).to_contain_text(FIRST_MESSAGE)
    expect(page.locator("#history-list")).to_contain_text(FIRST_MESSAGE)

    with page.expect_response(_is_attachment_upload) as upload_info:
        page.locator("#case-attachment-input").set_input_files(
            ATTACHMENT_PATH
        )
    upload_response = upload_info.value
    upload_body = upload_response.json()
    attachment_id = upload_body["id"]

    assert upload_response.status == 200
    assert upload_body["status"] == "review_required"
    expect(page.locator("#case-attachment-list")).to_contain_text(
        "selectable.pdf"
    )
    expect(page.locator(".attachment-command")).to_have_text("核对")
    page.locator(".attachment-command").click()
    expect(page.locator("#attachment-review-dialog")).to_be_visible()
    expect(page.locator("[data-review-block]")).to_have_value(
        "invoice total 299"
    )

    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlsplit(response.url).path
            == f"/api/attachments/{attachment_id}"
        )
    ) as confirm_info:
        page.locator("#attachment-review-confirm").click()
    assert confirm_info.value.status == 200
    expect(page.locator("#attachment-review-dialog")).to_be_hidden()
    expect(page.locator("#case-attachment-list")).to_contain_text(
        "已确认"
    )

    page.locator("#case-message").fill(FOLLOWUP_MESSAGE)
    with page.expect_request(_is_consult_request) as followup_request_info:
        with page.expect_response(
            _is_consult_response
        ) as followup_response_info:
            page.locator("#case-send").click()
    followup_request = followup_request_info.value
    followup_response = followup_response_info.value
    followup_request_body = followup_request.post_data_json
    followup_response_body = followup_response.json()

    assert followup_response.status == 200
    assert followup_request_body == {
        "message": FOLLOWUP_MESSAGE,
        "attachment_ids": [attachment_id],
        "session_id": session_id,
    }
    assert followup_response_body["session_id"] == session_id
    assert [
        attachment["id"]
        for attachment in followup_response_body["attachments"]
    ] == [attachment_id]
    expect(page.locator("#thread")).to_contain_text(FOLLOWUP_MESSAGE)
    expect(page.locator(".message-attachment")).to_have_count(1)
    expect(page.locator("#history-list")).to_contain_text(FIRST_MESSAGE)

    page.reload(wait_until="networkidle")

    expect(page.locator("#case-screen")).to_be_visible()
    expect(page.locator(".user-message")).to_have_count(2)
    expect(page.locator("#thread")).to_contain_text(FIRST_MESSAGE)
    expect(page.locator("#thread")).to_contain_text(FOLLOWUP_MESSAGE)
    expect(page.locator("#history-list")).to_contain_text(FIRST_MESSAGE)
    expect(page.locator(".message-attachment summary")).to_contain_text(
        "selectable.pdf"
    )
    page.locator(".message-attachment summary").click()
    expect(page.locator(".message-attachment-text")).to_be_visible()
    expect(page.locator(".message-attachment-text")).to_have_text(
        "invoice total 299"
    )
    _assert_no_account_bootstrap_requests(requested_paths)
    _assert_target_viewports(page)

    page.set_viewport_size({"width": 1366, "height": 768})
    page.locator(".history-item").hover()
    page.locator(".history-delete").click()
    expect(page.locator("#delete-dialog")).to_be_visible()
    with page.expect_response(
        lambda response: (
            response.request.method == "DELETE"
            and urlsplit(response.url).path
            == f"/api/sessions/{session_id}"
        )
    ) as delete_info:
        page.locator("#delete-confirm").click()

    assert delete_info.value.status == 204
    expect(page.locator("#new-screen")).to_be_visible()
    expect(page.locator("#history-list")).to_be_empty()
    expect(page.locator("#history-status")).to_have_text(
        "还没有咨询记录"
    )
    assert page.evaluate(
        """() => sessionStorage.getItem(
          "weiquan.current-session-id"
        )"""
    ) is None
    context.close()


@pytest.mark.parametrize(
    ("error_code", "expected_message", "retry_label"),
    [
        (
            "case_no_progress",
            (
                "当前信息下没有新的处理步骤。请补充对方回复、"
                "新材料、新事件或风险变化后再继续。"
            ),
            None,
        ),
        (
            "consultation_conflict",
            "这条咨询刚刚有了更新，请重新提交本次追问。",
            "重新提交",
        ),
    ],
)
def test_continuation_conflict_keeps_persisted_thread_and_draft(
    local_full_test_server: object,
    browser: Browser,
    error_code: str,
    expected_message: str,
    retry_label: str | None,
) -> None:
    del local_full_test_server
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    page.goto(BASE_URL, wait_until="networkidle")

    page.locator("#new-message").fill(FIRST_MESSAGE)
    with page.expect_response(_is_consult_response):
        page.locator("#new-send").click()
    expect(page.locator("#case-screen")).to_be_visible()
    initial_user_count = page.locator(".user-message").count()
    initial_assistant_count = page.locator(".assistant-message").count()
    draft = f"继续处理 {error_code}"

    def reject_continuation(route: object) -> None:
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps(
                {
                    "detail": {
                        "code": error_code,
                        "message": "服务器原始提示不应直接展示",
                    }
                },
                ensure_ascii=False,
            ),
        )

    page.route("**/api/consult", reject_continuation)
    page.locator("#case-message").fill(draft)
    with page.expect_response(_is_consult_response) as response_info:
        page.locator("#case-send").click()

    assert response_info.value.status == 409
    expect(page.locator(".user-message")).to_have_count(initial_user_count)
    expect(page.locator(".assistant-message")).to_have_count(
        initial_assistant_count
    )
    expect(page.locator("#case-message")).to_have_value(draft)
    expect(page.locator("#case-message")).to_be_enabled()
    expect(page.locator("#case-message")).to_be_focused()
    expect(page.locator("#case-send")).to_be_enabled()
    expect(page.locator("#case-alert")).to_have_attribute(
        "data-tone",
        "warning",
    )
    expect(page.locator("#case-alert")).to_contain_text(expected_message)
    retry = page.locator("#case-alert .case-alert-actions button")
    if retry_label is None:
        expect(retry).to_have_count(0)
    else:
        expect(retry).to_have_count(1)
        expect(retry).to_have_text(retry_label)
    context.close()


def test_legacy_internal_limitation_is_rendered_as_public_guidance(
    local_full_test_server: object,
    browser: Browser,
) -> None:
    del local_full_test_server
    context = browser.new_context(viewport={"width": 360, "height": 800})
    page = context.new_page()

    def add_legacy_limitation(route: object) -> None:
        upstream = route.fetch()
        payload = upstream.json()
        payload["limitations"] = [LEGACY_UNVERIFIED_LIMITATION]
        guidance = payload.get("guidance")
        if isinstance(guidance, dict):
            guidance["limitations"] = [LEGACY_UNVERIFIED_LIMITATION]
        route.fulfill(
            status=upstream.status,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    page.route("**/api/consult", add_legacy_limitation)
    page.goto(BASE_URL, wait_until="networkidle")
    page.locator("#new-message").fill(FIRST_MESSAGE)
    with page.expect_response(_is_consult_response):
        page.locator("#new-send").click()

    expect(page.locator("#thread")).to_contain_text(
        GENERAL_GUIDANCE_LIMITATION
    )
    for internal_text in ("本项目", "本地法条", "确定性规则"):
        expect(page.locator("#thread")).not_to_contain_text(internal_text)
    context.close()


def test_plan_update_reply_precedes_keyboard_accessible_collapsed_plan(
    local_full_test_server: object,
    browser: Browser,
) -> None:
    del local_full_test_server
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    page.goto(BASE_URL, wait_until="networkidle")

    page.locator("#new-message").fill(FIRST_MESSAGE)
    with page.expect_response(_is_consult_response):
        page.locator("#new-send").click()
    page.locator("#case-message").fill(FOLLOWUP_MESSAGE)
    with page.expect_response(_is_consult_response) as plan_response_info:
        page.locator("#case-send").click()
    base_response = plan_response_info.value.json()
    assert base_response["turn_kind"] == "initial_plan"

    update_text = "新增书面拒绝记录后，证据链更完整。"
    hidden_summary = "完整更新方案中的折叠内容"
    update_response = deepcopy(base_response)
    update_response.update(
        {
            "turn_id": str(uuid4()),
            "audit_id": str(uuid4()),
            "turn_kind": "plan_update",
            "reply": {
                "text": update_text,
                "suggested_actions": ["保存书面拒绝记录原件"],
                "citation_refs": [],
                "new_case": None,
            },
        }
    )
    update_response["plan"]["summary"] = hidden_summary

    def fulfill_plan_update(route: object) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(update_response, ensure_ascii=False),
        )

    page.route("**/api/consult", fulfill_plan_update)
    page.locator("#case-message").fill("房东已经书面拒绝退还押金")
    with page.expect_response(_is_consult_response):
        page.locator("#case-send").click()

    update_message = page.locator(
        '.assistant-message[data-turn-kind="plan_update"]'
    )
    expect(update_message).to_contain_text("方案已根据新信息更新")
    expect(update_message).to_contain_text(update_text)
    expect(update_message).to_contain_text("保存书面拒绝记录原件")
    details = update_message.locator(".plan-update-details")
    details_body = details.locator(".plan-update-details-body")
    summary = details.locator(":scope > summary")
    expect(details).not_to_have_attribute("open", "")
    expect(summary).to_have_text("查看完整更新方案")
    expect(details_body).to_be_hidden()

    summary.focus()
    expect(summary).to_be_focused()
    summary.press("Enter")
    expect(details).to_have_attribute("open", "")
    expect(details_body).to_be_visible()
    expect(details_body).to_contain_text(hidden_summary)
    expect(update_message.locator(".assistant-heading small")).to_have_text(
        "方案已整理"
    )
    _assert_target_viewports(page)
    context.close()


def _assert_no_account_bootstrap_requests(paths: list[str]) -> None:
    assert not [
        path for path in paths if path.startswith("/api/auth/")
    ]
    assert "/api/auth/csrf" not in paths
    assert not [
        path for path in paths if path.startswith("/api/trial/")
    ]
    assert not [
        path
        for path in paths
        if path == "/api/privacy"
        or path.startswith("/api/privacy/")
    ]


def _assert_target_viewports(page: Page) -> None:
    for width, height in ((1366, 768), (360, 800)):
        page.set_viewport_size({"width": width, "height": height})
        measurements = page.evaluate(
            """() => {
              const header = document.querySelector(".app-header");
              const items = [...header.children]
                .filter((item) => {
                  const style = getComputedStyle(item);
                  const rect = item.getBoundingClientRect();
                  return (
                    style.display !== "none" &&
                    style.visibility !== "hidden" &&
                    rect.width > 0 &&
                    rect.height > 0
                  );
                })
                .map((item) => {
                  const rect = item.getBoundingClientRect();
                  return {
                    name: item.className,
                    left: rect.left,
                    right: rect.right,
                    top: rect.top,
                    bottom: rect.bottom,
                  };
                });
              const overlaps = [];
              for (let left = 0; left < items.length; left += 1) {
                for (
                  let right = left + 1;
                  right < items.length;
                  right += 1
                ) {
                  const first = items[left];
                  const second = items[right];
                  if (
                    first.left < second.right - 0.5 &&
                    first.right > second.left + 0.5 &&
                    first.top < second.bottom - 0.5 &&
                    first.bottom > second.top + 0.5
                  ) {
                    overlaps.push([first.name, second.name]);
                  }
                }
              }
              return {
                documentOverflow:
                  document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
                bodyOverflow:
                  document.body.scrollWidth >
                  document.body.clientWidth,
                headerItemsInside: items.every(
                  (item) =>
                    item.left >= 0 &&
                    item.right <= window.innerWidth,
                ),
                overlaps,
              };
            }"""
        )
        assert measurements["documentOverflow"] is False
        assert measurements["bodyOverflow"] is False
        assert measurements["headerItemsInside"] is True
        assert measurements["overlaps"] == []


def _is_consult_request(request: object) -> bool:
    return (
        getattr(request, "method", None) == "POST"
        and urlsplit(getattr(request, "url", "")).path == "/api/consult"
    )


def _is_consult_response(response: object) -> bool:
    request = getattr(response, "request", None)
    return (
        getattr(request, "method", None) == "POST"
        and urlsplit(getattr(response, "url", "")).path == "/api/consult"
    )


def _is_attachment_upload(response: object) -> bool:
    request = getattr(response, "request", None)
    return (
        getattr(request, "method", None) == "POST"
        and urlsplit(getattr(response, "url", "")).path
        == "/api/attachments"
    )


def _wait_until_ready(thread: threading.Thread) -> None:
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if not thread.is_alive():
            pytest.fail("E2E test server exited before becoming ready")
        try:
            response = httpx.get(
                f"{BASE_URL}/health",
                timeout=0.5,
                trust_env=False,
            )
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.05)
    pytest.fail(f"E2E test server did not become ready: {last_error}")
