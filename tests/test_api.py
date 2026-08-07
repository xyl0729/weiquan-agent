from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_pipeline import make_pipeline


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
    assert body["verdict"]["code"] == "deduction_lacks_stated_basis"
    assert body["plan"]["rendered_text"].startswith("【立即保全证据】")
    assert len(body["citations"]) >= 7
    assert body["usage"]["provider"] == "fake"
    assert "api_key" not in second.text.casefold()
    assert "authorization" not in second.text.casefold()


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
    scripts = [
        client.get(f"/static/js/{name}.js")
        for name in ("api", "state", "render", "app")
    ]

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "维权咨询助手" in index.text
    assert 'src="/static/js/app.js"' in index.text
    assert 'href="/static/favicon.svg"' in index.text
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "--paper: #ffffff" in stylesheet.text
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert all(response.status_code == 200 for response in scripts)
    assert all(
        "javascript" in response.headers["content-type"]
        for response in scripts
    )

    script_source = "\n".join(response.text for response in scripts)
    assert "innerHTML" not in script_source
    assert "insertAdjacentHTML" not in script_source
    assert "localStorage" not in script_source
    assert "deepseek_api_key" not in script_source.casefold()
    assert "authorization" not in script_source.casefold()
    assert "sessionStorage" in script_source

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
