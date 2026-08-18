from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.agent.errors import (
    CaseNoProgressError,
    ConsultationConflictError,
)
from app.attachments.errors import AttachmentInputError
from app.attachments.models import ExtractionBlock, ExtractionResult
from app.attachments.service import AttachmentService
from app.attachments.store import AttachmentStore
from app.api.schemas import ConsultRequest, ConsultResponse
from app.config import Settings
from app.main import create_app
from tests.test_pipeline import make_pipeline


class ApiExtractionWorker:
    def __init__(
        self,
        *,
        error: Exception | None = None,
    ) -> None:
        self.error = error
        self.calls = 0

    async def extract(
        self,
        source_path: Path,
        **kwargs: Any,
    ) -> ExtractionResult:
        del source_path, kwargs
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ExtractionResult(
            media_type="application/pdf",
            page_count=1,
            extraction_method="direct_text",
            blocks=(
                ExtractionBlock(
                    page_number=1,
                    block_index=0,
                    text="invoice total 299",
                    confidence=0.98,
                ),
            ),
            warnings=("review_amount",),
        )


def make_attachment_client(
    tmp_path: Path,
    attachment_temp_dir: Path,
    *,
    worker: ApiExtractionWorker | None = None,
    ocr_ready: bool = True,
    max_context_chars: int = 12_000,
) -> tuple[TestClient, object, AttachmentStore, ApiExtractionWorker]:
    pipeline, _ = make_pipeline(tmp_path)
    settings = pipeline.settings.model_copy(
        update={
            "attachment_temp_dir": attachment_temp_dir,
            "max_attachment_context_chars": max_context_chars,
        }
    )
    attachments = pipeline.attachments
    active_worker = worker or ApiExtractionWorker()
    service = AttachmentService(
        attachments,
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
        worker=active_worker,
    )
    application = create_app(settings, pipeline=pipeline)
    application.state.attachment_store = attachments
    application.state.attachment_service = service
    application.state.ocr_ready = ocr_ready
    return TestClient(application), application, attachments, active_worker


def upload_pdf(client: TestClient):
    return client.post(
        "/api/attachments",
        files={
            "file": (
                "invoice.pdf",
                b"%PDF-1.7\nsafe api fixture",
                "application/pdf",
            )
        },
    )


def test_consult_api_followup_and_ready_response(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    first = client.post(
        "/api/consult",
        json={"message": "房东不退押金"},
    )
    assert first.status_code == 200
    session_id = first.json()["session_id"]
    assert first.json()["status"] == "need_more_facts"
    assert first.json()["turn_kind"] == "fact_collection"
    assert first.json()["reply"] is not None
    assert "房东不退押金" in first.json()["reply"]["text"]
    assert first.json()["reply"]["citation_refs"] == []
    assert first.json()["attachments"] == []

    second = client.post(
        "/api/consult",
        json={
            "session_id": session_id,
            "message": (
                "押金2000元，房东扣2000元，没理由，"
                "合同没写可以扣。"
            ),
            "jurisdiction": "CN",
        },
    )
    body = second.json()

    assert second.status_code == 200
    assert body["status"] == "ready"
    assert body["turn_kind"] == "initial_plan"
    assert body["reply"] is None
    assert body["verdict"]["code"] == "deduction_lacks_stated_basis"
    assert body["plan"]["communication_guide"]["recipient"]
    assert (
        body["plan"]["communication_text"]
        == body["plan"]["communication_guide"]["message"]
    )
    assert body["plan"]["rendered_text"].startswith("【立即保全证据】")
    assert len(body["citations"]) >= 7
    assert body["attachments"] == []
    assert body["usage"]["provider"] == "fake"
    assert "api_key" not in second.text.casefold()
    assert "authorization" not in second.text.casefold()


@pytest.mark.parametrize(
    ("error_type", "expected_code", "expected_message"),
    [
        (
            CaseNoProgressError,
            "case_no_progress",
            (
                "当前信息下没有新的处理步骤；请补充对方回复、"
                "新材料、新事件或风险变化后再继续"
            ),
        ),
        (
            ConsultationConflictError,
            "consultation_conflict",
            "会话刚刚发生更新，请重新提交本次追问",
        ),
    ],
)
def test_consult_progression_conflicts_use_stable_409_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    expected_code: str,
    expected_message: str,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)

    async def reject_consultation(**kwargs: Any) -> None:
        del kwargs
        raise error_type()

    monkeypatch.setattr(pipeline, "consult", reject_consultation)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    response = client.post(
        "/api/consult",
        json={"message": "继续"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": expected_code,
            "message": expected_message,
        }
    }


def test_consult_api_rejects_public_provider_selection(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    response = client.post(
        "/api/consult",
        json={
            "message": "房东不退押金",
            "provider_id": "fake",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "request_validation",
            "message": "请求字段无效",
        }
    }


def test_provider_catalog_api_exposes_only_safe_status(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    settings = pipeline.settings.model_copy(
        update={"deepseek_api_key": "do-not-expose"}
    )
    client = TestClient(create_app(settings, pipeline=pipeline))

    response = client.get("/api/providers")

    assert response.status_code == 200
    assert response.json() == {
        "providers": [
            {
                "id": "deepseek",
                "display_name": "DeepSeek",
                "model": settings.deepseek_model,
                "available": True,
                "unavailable_reason": None,
                "offline": False,
                "is_default": True,
            },
        ]
    }
    assert "do-not-expose" not in response.text


def test_invalid_body_does_not_echo_secret_like_input(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )
    secret = "sk-abcdefghijklmnop"

    response = client.post(
        "/api/consult",
        json={
            "message": "房东不退押金",
            "deepseek_api_key": secret,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation"
    assert secret not in response.text


def test_unknown_session_maps_to_safe_422(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    response = client.post(
        "/api/consult",
        json={
            "session_id": str(uuid4()),
            "message": "房东不退押金",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "session_not_found",
            "message": "会话不存在或已过期",
        }
    }
    assert "traceback" not in response.text.casefold()


@pytest.mark.parametrize(
    "turn_kind",
    [
        "fact_collection",
        "initial_plan",
        "plan_update",
        "followup_answer",
        "new_case",
        "unverified_guidance",
        "emergency_guidance",
    ],
)
def test_consult_response_accepts_valid_turn_kind_combinations(
    turn_kind: str,
) -> None:
    payload = _response_for_turn_kind(turn_kind)

    response = ConsultResponse.model_validate(payload)

    assert response.turn_kind == turn_kind
    assert response.attachments == []


def test_consult_response_accepts_unverified_coverage_for_safe_clarification(
) -> None:
    payload = _response_for_turn_kind("fact_collection")
    payload["coverage"] = {
        "mode": "unverified_guidance",
        "topic_id": "medical_service_dispute",
        "topic_label": "医疗服务与病历争议",
        "confidence": 0.86,
        "playbook_id": None,
        "notice": "当前仅保留已识别主题，补充事实后再继续判断。",
        "risk_flags": [],
    }

    response = ConsultResponse.model_validate(payload)

    assert response.turn_kind == "fact_collection"
    assert response.guidance is None
    assert response.coverage is not None
    assert response.coverage.mode == "unverified_guidance"
    assert response.coverage.topic_id == "medical_service_dispute"


def test_safe_unverified_clarification_rejects_citations() -> None:
    payload = _response_for_turn_kind("fact_collection")
    payload["coverage"] = {
        "mode": "unverified_guidance",
        "topic_id": "medical_service_dispute",
        "topic_label": "医疗服务与病历争议",
        "confidence": 0.86,
        "playbook_id": None,
        "notice": "当前仅保留已识别主题，补充事实后再继续判断。",
        "risk_flags": [],
    }
    payload["citations"] = [
        {
            "ref": "invented",
            "law_name": "虚构法",
            "article_no": "第一条",
            "content": "虚构内容",
            "effective_date": "2026-08-08",
            "source_url": "https://example.com",
        }
    ]

    with pytest.raises(ValidationError):
        ConsultResponse.model_validate(payload)


def test_consult_request_attachment_ids_default_and_validation() -> None:
    request = ConsultRequest(message="房东不退押金")
    attachment_id = uuid4()

    assert request.attachment_ids == []

    with pytest.raises(ValidationError):
        ConsultRequest(
            message="房东不退押金",
            attachment_ids=[attachment_id, attachment_id],
        )
    with pytest.raises(ValidationError):
        ConsultRequest(
            message="房东不退押金",
            attachment_ids=[uuid4() for _ in range(4)],
        )
    with pytest.raises(ValidationError):
        ConsultRequest(
            message="房东不退押金",
            attachment_ids=["not-a-uuid"],
        )


@pytest.mark.parametrize(
    "attachment_ids",
    [
        ["not-a-uuid"],
        [str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())],
    ],
)
def test_consult_api_rejects_invalid_attachment_ids(
    tmp_path: Path,
    attachment_ids: list[str],
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    response = client.post(
        "/api/consult",
        json={
            "message": "房东不退押金",
            "attachment_ids": attachment_ids,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation"


@pytest.mark.parametrize(
    ("turn_kind", "invalid_case"),
    [
        ("fact_collection", "empty_collection"),
        ("fact_collection", "reply_with_citation"),
        ("initial_plan", "missing_plan"),
        ("initial_plan", "unexpected_reply"),
        ("plan_update", "missing_verdict"),
        ("followup_answer", "missing_reply"),
        ("followup_answer", "unexpected_plan"),
        ("new_case", "missing_new_case"),
    ],
)
def test_consult_response_rejects_invalid_turn_kind_combinations(
    turn_kind: str,
    invalid_case: str,
) -> None:
    payload = _response_for_turn_kind(turn_kind)
    if invalid_case == "empty_collection":
        payload.update({"questions": [], "limitations": []})
    elif invalid_case == "reply_with_citation":
        payload["reply"] = _reply_payload(citation_refs=["invented"])
    elif invalid_case == "unexpected_reply":
        payload["reply"] = _reply_payload()
    elif invalid_case == "missing_plan":
        payload["plan"] = None
    elif invalid_case == "missing_verdict":
        payload["verdict"] = None
    elif invalid_case == "missing_reply":
        payload["reply"] = None
    elif invalid_case == "unexpected_plan":
        payload.update(
            {
                "plan": _plan_payload(),
                "verdict": _verdict_payload(),
            }
        )
    elif invalid_case == "missing_new_case":
        payload["reply"] = _reply_payload()

    with pytest.raises(ValidationError):
        ConsultResponse.model_validate(payload)


def test_consult_response_requires_reply_citations_to_match_top_level() -> None:
    payload = _response_for_turn_kind("followup_answer")
    payload["reply"] = _reply_payload(
        citation_refs=["消费者权益保护法.第二十四条"]
    )

    with pytest.raises(ValidationError):
        ConsultResponse.model_validate(payload)


@pytest.mark.parametrize(
    "turn_kind",
    ["unverified_guidance", "emergency_guidance"],
)
def test_guidance_turn_rejects_formal_artifacts_and_case_citations(
    turn_kind: str,
) -> None:
    payload = _response_for_turn_kind(turn_kind)
    payload.update(
        {
            "plan": _plan_payload(),
            "verdict": _verdict_payload(),
            "citations": [
                {
                    "ref": "invented",
                    "law_name": "虚构法",
                    "article_no": "第一条",
                    "content": "虚构内容",
                    "effective_date": "2026-08-08",
                    "source_url": "https://example.com",
                }
            ],
        }
    )

    with pytest.raises(ValidationError):
        ConsultResponse.model_validate(payload)


def test_unverified_guidance_allows_general_citations() -> None:
    payload = _response_for_turn_kind("unverified_guidance")
    payload["citations"] = [
        {
            "ref": "民法典.第五百七十七条",
            "law_name": "中华人民共和国民法典",
            "article_no": "第五百七十七条",
            "content": "当事人一方不履行合同义务，应当承担违约责任。",
            "effective_date": "2021-01-01",
            "source_url": "https://example.com/law",
            "basis_scope": "general",
            "applicability_notice": "是否适用于本案仍需结合具体事实核对。",
        }
    ]

    response = ConsultResponse.model_validate(payload)

    assert response.citations[0].basis_scope == "general"


def _response_for_turn_kind(turn_kind: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "audit_id": str(uuid4()),
        "followup_round": 0,
        "can_ask_more": False,
        "status": "ready",
        "turn_kind": turn_kind,
        "coverage": None,
        "guidance": None,
        "verdict": None,
        "plan": None,
        "reply": None,
        "questions": [],
        "limitations": [],
        "citations": [],
        "usage": {
            "provider": "fake",
            "model": "fake-extractor-v1",
            "request_id": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": None,
        },
    }
    if turn_kind == "fact_collection":
        payload.update(
            {
                "status": "need_more_facts",
                "can_ask_more": True,
                "questions": ["请补充关键事实"],
            }
        )
    elif turn_kind in {"initial_plan", "plan_update"}:
        payload.update(
            {
                "verdict": _verdict_payload(),
                "plan": _plan_payload(),
            }
        )
    elif turn_kind == "followup_answer":
        payload["reply"] = _reply_payload()
    elif turn_kind == "new_case":
        payload["reply"] = _reply_payload(
            text="这看起来是另一项纠纷，建议单独建立咨询。",
            new_case={
                "scenario_id": "return_refused",
                "label": "退货换货被拒",
            },
        )
    elif turn_kind in {
        "unverified_guidance",
        "emergency_guidance",
    }:
        emergency = turn_kind == "emergency_guidance"
        limitations = ["本结果不构成正式法律结论"]
        payload.update(
            {
                "status": "escalate" if emergency else "need_more_facts",
                "can_ask_more": not emergency,
                "coverage": {
                    "mode": turn_kind,
                    "topic_id": "education_minor_safety",
                    "topic_label": "教育、未成年人和校园安全",
                    "confidence": 0.9,
                    "playbook_id": None,
                    "notice": "当前提供受限指导。",
                    "risk_flags": ["minor_harm"] if emergency else [],
                },
                "guidance": {
                    "direct_answer": None,
                    "evidence_now": ["保存现有沟通记录"],
                    "actions": ["先确保人身安全"],
                    "communication_guide": {
                        "recipient": "学校负责人",
                        "channels": ["学校官方邮箱"],
                        "when_to_send": "整理现有事实后尽快发送",
                        "objective": "要求书面确认收悉并说明处理安排",
                        "message": "您好，我想书面反映相关情况，请确认收悉。",
                        "after_sending": ["保存发送和回复记录"],
                        "escalation": ["没有回应时向主管机构求助"],
                        "required_before_send": [],
                    },
                    "limitations": limitations,
                    "next_question": (
                        None if emergency else "事件发生在什么时候？"
                    ),
                },
                "limitations": limitations,
            }
        )
    return payload


def _verdict_payload() -> dict[str, object]:
    return {
        "code": "test_verdict",
        "label": "测试判断",
        "status": "ready",
        "rule_ids": ["test_rule"],
        "key_point": "测试判断要点",
    }


def _plan_payload() -> dict[str, object]:
    return {
        "summary": "测试方案摘要",
        "evidence_now": ["保存现有证据"],
        "actions": ["先书面沟通"],
        "communication_text": "请书面说明处理结果。",
        "limitations": [],
        "time_limit": None,
        "jurisdiction": {
            "code": "CN",
            "name": "中国大陆",
            "status": "supported",
            "small_claim_threshold_yuan": None,
            "notices": [],
        },
        "rendered_text": "测试渲染文本",
        "evidence_request_text": "测试证据清单",
    }


def _reply_payload(
    *,
    text: str = "先固定对方拒绝处理的证据，再按原方案推进。",
    citation_refs: list[str] | None = None,
    new_case: dict[str, str | None] | None = None,
) -> dict[str, object]:
    return {
        "text": text,
        "suggested_actions": [],
        "citation_refs": citation_refs or [],
        "new_case": new_case,
    }


def test_overlong_message_uses_safe_validation_error(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    response = client.post(
        "/api/consult",
        json={"message": "x" * (pipeline.settings.max_message_length + 1)},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation"


def test_health_reports_local_dependencies_without_secrets(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["checks"]["provider"] == "offline"
    assert "key" not in response.text.casefold()


def test_attachment_api_upload_review_confirm_delete_and_cors(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    client, _, _, worker = make_attachment_client(
        tmp_path,
        project_attachment_temp_dir,
    )

    uploaded = upload_pdf(client)

    assert uploaded.status_code == 200
    body = uploaded.json()
    attachment_id = body["id"]
    assert body == {
        "id": attachment_id,
        "status": "review_required",
        "original_name": "invoice.pdf",
        "media_type": "application/pdf",
        "size_bytes": len(b"%PDF-1.7\nsafe api fixture"),
        "page_count": 1,
        "extraction_method": "direct_text",
        "blocks": [
            {
                "page_number": 1,
                "block_index": 0,
                "text": "invoice total 299",
                "confidence": 0.98,
            }
        ],
        "warnings": ["review_amount"],
        "confirmed_text": None,
        "error_code": None,
    }
    assert worker.calls == 1

    fetched = client.get(f"/api/attachments/{attachment_id}")
    confirmed = client.patch(
        f"/api/attachments/{attachment_id}",
        json={"confirmed_text": "  invoice total 399\n"},
    )
    preflight = client.options(
        f"/api/attachments/{attachment_id}",
        headers={
            "Origin": "http://localhost:8000",
            "Access-Control-Request-Method": "PATCH",
        },
    )
    deleted = client.delete(f"/api/attachments/{attachment_id}")

    assert fetched.status_code == 200
    assert fetched.json() == body
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["confirmed_text"] == "invoice total 399"
    assert preflight.status_code == 200
    assert "PATCH" in preflight.headers["access-control-allow-methods"]
    assert deleted.status_code == 204
    assert deleted.content == b""

    for response in (uploaded, fetched, confirmed, deleted):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in response.headers[
            "content-security-policy"
        ]

    public_text = uploaded.text.casefold()
    for private_name in (
        "sha256",
        "reservation_id",
        "session_id",
        "turn_id",
        "expires_at",
        "created_at",
        "updated_at",
        "source_path",
        "traceback",
    ):
        assert private_name not in public_text

    deleted_again = client.delete(f"/api/attachments/{attachment_id}")
    missing = client.get(f"/api/attachments/{attachment_id}")
    assert deleted_again.status_code == 204
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "attachment_not_found"


def test_confirmed_upload_can_be_used_by_consultation(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    client, application, attachments, _ = make_attachment_client(
        tmp_path,
        project_attachment_temp_dir,
    )
    uploaded = upload_pdf(client)
    attachment_id = uploaded.json()["id"]
    confirmed = client.patch(
        f"/api/attachments/{attachment_id}",
        json={"confirmed_text": "平台订单金额299元"},
    )

    response = client.post(
        "/api/consult",
        json={
            "message": (
                "押金2000元，房东扣2000元，没理由，"
                "合同没写可以扣。"
            ),
            "attachment_ids": [attachment_id],
        },
    )

    assert confirmed.status_code == 200
    assert response.status_code == 200
    body = response.json()
    assert body["turn_kind"] == "initial_plan"
    assert body["attachments"] == [
        {
            "id": attachment_id,
            "status": "bound",
            "original_name": "invoice.pdf",
            "media_type": "application/pdf",
            "size_bytes": len(b"%PDF-1.7\nsafe api fixture"),
            "page_count": 1,
            "extraction_method": "direct_text",
            "warnings": ["review_amount"],
            "confirmed_text": "平台订单金额299元",
        }
    ]
    assert [
        item.id for item in attachments.list_for_turn(body["turn_id"])
    ] == [attachment_id]
    stored_turn = attachments.sessions.list_turns(body["session_id"])[0]
    assert stored_turn.response["attachments"] == []
    assert (
        application.state.attachment_store
        is application.state.consultation_pipeline.attachments
    )


def test_attachment_api_returns_safe_failed_public_object(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    worker = ApiExtractionWorker(
        error=AttachmentInputError("attachment_corrupt")
    )
    client, _, _, _ = make_attachment_client(
        tmp_path,
        project_attachment_temp_dir,
        worker=worker,
    )

    response = upload_pdf(client)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "attachment_corrupt"
    assert response.json()["blocks"] == []
    assert "traceback" not in response.text.casefold()
    assert "rapidocr" not in response.text.casefold()
    assert str(tmp_path).casefold() not in response.text.casefold()


def test_attachment_api_validation_state_and_idempotence(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    client, _, attachments, _ = make_attachment_client(
        tmp_path,
        project_attachment_temp_dir,
        max_context_chars=8,
    )
    uploaded = upload_pdf(client)
    attachment_id = uploaded.json()["id"]

    blank = client.patch(
        f"/api/attachments/{attachment_id}",
        json={"confirmed_text": "   "},
    )
    extra = client.patch(
        f"/api/attachments/{attachment_id}",
        json={"confirmed_text": "invoice", "sha256": "secret"},
    )
    overlong = client.patch(
        f"/api/attachments/{attachment_id}",
        json={"confirmed_text": "123456789"},
    )
    invalid_id = client.get("/api/attachments/not-a-uuid")
    unknown_id = str(uuid4())
    unknown_get = client.get(f"/api/attachments/{unknown_id}")
    unknown_patch = client.patch(
        f"/api/attachments/{unknown_id}",
        json={"confirmed_text": "invoice"},
    )
    unknown_delete = client.delete(f"/api/attachments/{unknown_id}")

    assert blank.status_code == 422
    assert blank.json()["detail"]["code"] == "request_validation"
    assert extra.status_code == 422
    assert extra.json()["detail"]["code"] == "request_validation"
    assert overlong.status_code == 413
    assert overlong.json()["detail"]["code"] == (
        "attachment_context_too_long"
    )
    assert invalid_id.status_code == 422
    assert invalid_id.json()["detail"]["code"] == "request_validation"
    assert unknown_get.status_code == 404
    assert unknown_patch.status_code == 404
    assert unknown_delete.status_code == 204

    failed = attachments.create_processing(
        original_name="failed.pdf",
        media_type="application/pdf",
        size_bytes=10,
        sha256="a" * 64,
    )
    attachments.save_failure(failed.id, "attachment_corrupt")
    failed_patch = client.patch(
        f"/api/attachments/{failed.id}",
        json={"confirmed_text": "invoice"},
    )
    assert failed_patch.status_code == 409
    assert failed_patch.json()["detail"]["code"] == (
        "attachment_not_reviewable"
    )

    confirmed = client.patch(
        f"/api/attachments/{attachment_id}",
        json={"confirmed_text": "invoice"},
    )
    assert confirmed.status_code == 200
    reservation_id = attachments.reserve([attachment_id])
    reserved_delete = client.delete(
        f"/api/attachments/{attachment_id}"
    )
    assert reserved_delete.status_code == 409
    assert reserved_delete.json()["detail"]["code"] == (
        "attachment_already_bound"
    )

    session = attachments.sessions.create_session()
    turn = attachments.sessions.add_turn(
        session.id,
        user_message="review invoice",
        facts={},
        rule_matches=[],
        response={"status": "need_more_facts"},
    )
    attachments.bind_reserved(
        reservation_id,
        session_id=session.id,
        turn_id=turn.id,
        expected_ids=[attachment_id],
    )
    bound_delete = client.delete(f"/api/attachments/{attachment_id}")
    assert bound_delete.status_code == 409
    assert bound_delete.json()["detail"]["code"] == (
        "attachment_already_bound"
    )

    expired = attachments.create_processing(
        original_name="expired.pdf",
        media_type="application/pdf",
        size_bytes=10,
        sha256="b" * 64,
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    expired_get = client.get(f"/api/attachments/{expired.id}")
    expired_delete = client.delete(f"/api/attachments/{expired.id}")
    assert expired_get.status_code == 404
    assert expired_delete.status_code == 204


def test_ocr_unavailable_only_disables_upload(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
) -> None:
    client, _, attachments, worker = make_attachment_client(
        tmp_path,
        project_attachment_temp_dir,
        ocr_ready=False,
    )
    review = attachments.create_processing(
        original_name="existing.pdf",
        media_type="application/pdf",
        size_bytes=10,
        sha256="c" * 64,
    )
    attachments.save_extraction(
        review.id,
        ExtractionResult(
            media_type="application/pdf",
            page_count=1,
            extraction_method="direct_text",
            blocks=(
                ExtractionBlock(
                    page_number=1,
                    block_index=0,
                    text="existing text",
                    confidence=1,
                ),
            ),
        ),
    )

    unavailable = upload_pdf(client)
    health = client.get("/health")
    existing = client.get(f"/api/attachments/{review.id}")
    confirmed = client.patch(
        f"/api/attachments/{review.id}",
        json={"confirmed_text": "existing text"},
    )
    consultation = client.post(
        "/api/consult",
        json={"message": "My landlord kept the deposit."},
    )

    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == (
        "attachment_service_unavailable"
    )
    assert worker.calls == 0
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["checks"]["ocr"] == "unavailable"
    assert existing.status_code == 200
    assert confirmed.status_code == 200
    assert consultation.status_code == 200


def test_startup_recovers_processing_and_caches_ocr_readiness(
    tmp_path: Path,
    project_attachment_temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, sessions = make_pipeline(tmp_path)
    settings = pipeline.settings.model_copy(
        update={"attachment_temp_dir": project_attachment_temp_dir}
    )
    attachments = AttachmentStore(
        sessions,
        draft_ttl_seconds=settings.attachment_draft_ttl_seconds,
    )
    processing = attachments.create_processing(
        original_name="interrupted.pdf",
        media_type="application/pdf",
        size_bytes=10,
        sha256="d" * 64,
    )
    probes = 0

    def probe() -> bool:
        nonlocal probes
        probes += 1
        return True

    monkeypatch.setattr("app.main.probe_ocr_readiness", probe)
    application = create_app(settings, pipeline=pipeline)

    with TestClient(application) as client:
        first = client.get("/health")
        second = client.get("/health")

        assert first.json()["checks"]["ocr"] == "ok"
        assert second.json()["checks"]["ocr"] == "ok"
        assert application.state.attachment_store.sessions is sessions
        assert application.state.attachment_service.store is (
            application.state.attachment_store
        )

    recovered = application.state.attachment_store.get(processing.id)
    assert recovered.status == "failed"
    assert recovered.error_code == "attachment_service_unavailable"
    assert probes == 1


def test_web_index_static_assets_and_security_headers(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    index = client.get("/")
    stylesheet = client.get("/static/styles.css")
    favicon = client.get("/static/favicon.svg")
    script_names = (
        "api",
        "state",
        "auth",
        "captcha",
        "privacy",
        "render",
        "app",
    )
    scripts = [
        client.get(f"/static/js/{name}.js")
        for name in script_names
    ]

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "维权咨询助手" in index.text
    assert 'src="/static/js/app.js"' in index.text
    assert 'href="/static/favicon.svg"' in index.text
    for source in ("new", "case"):
        for control in (
            "attachment-input",
            "attachment-trigger",
            "attachment-list",
            "attachment-blocker",
        ):
            assert f'id="{source}-{control}"' in index.text
    for control in (
        "dialog",
        "form",
        "pages",
        "count",
        "confirm",
    ):
        assert f'id="attachment-review-{control}"' in index.text
    assert "provider-options" not in index.text
    assert "provider-retry" not in index.text
    assert "provider-toolbar" not in index.text
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "--paper: #ffffff" in stylesheet.text
    assert (
        "grid-template-columns: 18px minmax(0, 1fr) 18px;"
        in stylesheet.text
    )
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert all(response.status_code == 200 for response in scripts)
    assert all(
        "javascript" in response.headers["content-type"]
        for response in scripts
    )

    script_sources = {
        name: response.text
        for name, response in zip(script_names, scripts, strict=True)
    }
    script_source = "\n".join(script_sources.values())
    assert "innerHTML" not in script_source
    assert "insertAdjacentHTML" not in script_source
    assert "localStorage" not in script_source
    assert "deepseek_api_key" not in script_source.casefold()
    assert "authorization" not in script_source.casefold()
    assert "sessionStorage" in script_source
    for contract_marker in (
        '"/api/auth/captcha-config"',
        '"/api/auth/login"',
        '"/api/auth/logout"',
        '"/api/trial/start"',
        '"/api/trial/consult"',
        '"/api/privacy"',
        "unverified_guidance",
        "emergency_guidance",
        "communication_guide",
        "request_id",
        "estimated_cost_usd",
        '"/api/attachments"',
        "attachment_ids",
        "FormData",
    ):
        assert contract_marker in script_source
    state_source = script_sources["state"]
    assert "ATTACHMENT_DRAFTS_KEY" in state_source
    assert "cleanUuidList" in state_source
    assert "clearAuthenticatedState" in state_source
    for forbidden_ledger_field in (
        "confirmed_text",
        "original_name",
        "blocks",
        "warnings",
    ):
        assert forbidden_ledger_field not in state_source

    for response in (index, stylesheet, favicon, *scripts):
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "camera=()" in response.headers["permissions-policy"]


def test_session_history_list_detail_and_delete(
    tmp_path: Path,
) -> None:
    pipeline, store = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )
    store.create_session()

    first = client.post(
        "/api/consult",
        json={"message": "房东不退押金"},
    )
    session_id = first.json()["session_id"]
    client.post(
        "/api/consult",
        json={
            "session_id": session_id,
            "message": (
                "押金2000元，房东扣2000元，没理由，"
                "合同没写可以扣。"
            ),
            "jurisdiction": "CN",
        },
    )

    listed = client.get("/api/sessions")

    assert listed.status_code == 200
    assert len(listed.json()["sessions"]) == 1
    summary = listed.json()["sessions"][0]
    assert summary["session_id"] == session_id
    assert summary["title"] == "房东不退押金"
    assert summary["status"] == "ready"

    detail = client.get(f"/api/sessions/{session_id}")

    assert detail.status_code == 200
    assert detail.json()["session"] == summary
    assert len(detail.json()["turns"]) == 2
    assert detail.json()["turns"][0]["response"]["status"] == (
        "need_more_facts"
    )
    assert '"facts"' not in detail.text
    assert "rule_matches" not in detail.text
    assert "audit_records" not in detail.text

    deleted = client.delete(f"/api/sessions/{session_id}")
    deleted_again = client.delete(f"/api/sessions/{session_id}")

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert deleted_again.status_code == 204
    assert client.get("/api/sessions").json() == {"sessions": []}


def test_unknown_history_session_uses_safe_error(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )

    response = client.get(f"/api/sessions/{uuid4()}")

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "session_not_found",
            "message": "会话不存在或已过期",
        }
    }
    assert "traceback" not in response.text.casefold()


def test_history_api_rejects_corrupt_stored_response(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    client = TestClient(
        create_app(
            pipeline.settings,
            pipeline=pipeline,
        )
    )
    created = client.post(
        "/api/consult",
        json={"message": "房东不退押金"},
    )
    session_id = created.json()["session_id"]
    with sqlite3.connect(pipeline.settings.database_path) as connection:
        connection.execute(
            """
            UPDATE turns
            SET response_json = '{"status":"ready"}'
            WHERE session_id = ?
            """,
            (session_id,),
        )

    response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == (
        "session_response_invalid"
    )
    assert "traceback" not in response.text.casefold()
