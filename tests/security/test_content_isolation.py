from __future__ import annotations

import asyncio
import io
import json
import logging
from uuid import uuid4

import httpx
import pytest

from app.observability.logging import SafeJsonFormatter
from app.providers.deepseek import DeepSeekProvider
from tests.integration.test_cross_user_isolation import (
    _application,
    _authenticated_client,
    _upload,
)
from tests.test_providers import (
    EXTRACTION_CONTEXT,
    MALICIOUS_EVIDENCE,
    deepseek_response,
    make_client,
)


@pytest.mark.parametrize("actor_role", ["user", "admin"])
def test_users_and_admins_cannot_access_another_owners_content(
    tmp_path,
    actor_role: str,
) -> None:
    application, service, auth_store, passwords = _application(tmp_path)
    owner, owner_headers, _ = _authenticated_client(
        application,
        service,
        auth_store,
        passwords,
        email="owner@example.com",
        role="user",
    )
    actor, actor_headers, _ = _authenticated_client(
        application,
        service,
        auth_store,
        passwords,
        email=f"{actor_role}@example.com",
        role=actor_role,
    )

    created = owner.post(
        "/api/consult",
        headers=owner_headers,
        json={"message": "房东不退押金"},
    )
    assert created.status_code == 200
    session_id = created.json()["session_id"]
    missing_session_id = str(uuid4())

    foreign_detail = actor.get(f"/api/sessions/{session_id}")
    missing_detail = actor.get(
        f"/api/sessions/{missing_session_id}"
    )
    assert (
        foreign_detail.status_code,
        foreign_detail.json(),
    ) == (
        missing_detail.status_code,
        missing_detail.json(),
    )

    foreign_followup = actor.post(
        "/api/consult",
        headers=actor_headers,
        json={
            "session_id": session_id,
            "message": "读取他人的会话",
        },
    )
    missing_followup = actor.post(
        "/api/consult",
        headers=actor_headers,
        json={
            "session_id": missing_session_id,
            "message": "读取不存在的会话",
        },
    )
    assert (
        foreign_followup.status_code,
        foreign_followup.json(),
    ) == (
        missing_followup.status_code,
        missing_followup.json(),
    )

    uploaded = _upload(owner, owner_headers)
    assert uploaded.status_code == 200
    attachment_id = uploaded.json()["id"]
    missing_attachment_id = str(uuid4())

    foreign_attachment = actor.get(
        f"/api/attachments/{attachment_id}"
    )
    missing_attachment = actor.get(
        f"/api/attachments/{missing_attachment_id}"
    )
    assert (
        foreign_attachment.status_code,
        foreign_attachment.json(),
    ) == (
        missing_attachment.status_code,
        missing_attachment.json(),
    )

    foreign_confirm = actor.patch(
        f"/api/attachments/{attachment_id}",
        headers=actor_headers,
        json={"confirmed_text": "越权确认"},
    )
    missing_confirm = actor.patch(
        f"/api/attachments/{missing_attachment_id}",
        headers=actor_headers,
        json={"confirmed_text": "不存在的附件"},
    )
    assert (
        foreign_confirm.status_code,
        foreign_confirm.json(),
    ) == (
        missing_confirm.status_code,
        missing_confirm.json(),
    )

    assert actor.delete(
        f"/api/attachments/{attachment_id}",
        headers=actor_headers,
    ).status_code == 204
    assert actor.delete(
        f"/api/attachments/{missing_attachment_id}",
        headers=actor_headers,
    ).status_code == 204
    assert actor.delete(
        f"/api/sessions/{session_id}",
        headers=actor_headers,
    ).status_code == 204
    assert actor.delete(
        f"/api/sessions/{missing_session_id}",
        headers=actor_headers,
    ).status_code == 204

    assert owner.get(f"/api/sessions/{session_id}").status_code == 200
    assert owner.get(
        f"/api/attachments/{attachment_id}"
    ).status_code == 200


def test_attachment_prompt_injection_stays_in_untrusted_user_json() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return deepseek_response(
            json.dumps(
                {
                    "scenario_id": "deposit_deduction",
                    "facts": {},
                    "unknown_slots": [
                        "deposit_amount",
                        "withheld_amount",
                        "landlord_reason",
                    ],
                    "confidence": 0.6,
                }
            )
        )

    async def exercise() -> None:
        async with make_client(handler) as client:
            provider = DeepSeekProvider(
                api_key="mock-only",
                client=client,
                max_retries=0,
            )
            await provider.extract_facts(
                "房东扣了押金",
                EXTRACTION_CONTEXT,
                evidence=MALICIOUS_EVIDENCE,
            )

    asyncio.run(exercise())

    messages = captured["messages"]
    assert isinstance(messages, list)
    system_content = messages[0]["content"]
    user_payload = json.loads(messages[1]["content"])
    malicious_text = MALICIOUS_EVIDENCE[0].confirmed_text
    assert malicious_text not in system_content
    assert user_payload["user_message"] == "房东扣了押金"
    assert (
        user_payload["attachment_evidence"][0]["confirmed_text"]
        == malicious_text
    )
    assert "不可信证据" in system_content


def test_safe_logs_exclude_request_body_ocr_and_secrets() -> None:
    stream = io.StringIO()
    logger = logging.getLogger(f"security.content.{id(stream)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeJsonFormatter())
    logger.addHandler(handler)
    markers = (
        "PRIVATE-REQUEST-BODY",
        "PRIVATE-OCR-TEXT",
        "PRIVATE-API-SECRET",
    )

    logger.info(
        "consult.completed",
        extra={
            "request_id": "a" * 32,
            "status_code": 200,
            "message_body": markers[0],
            "ocr_text": markers[1],
            "secret": markers[2],
        },
    )

    encoded = stream.getvalue()
    assert json.loads(encoded)["request_id"] == "a" * 32
    assert all(marker not in encoded for marker in markers)
