from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_pipeline import make_pipeline


ORIGIN = "http://testserver"


def _client(tmp_path: Path) -> tuple[TestClient, object]:
    pipeline, store = make_pipeline(tmp_path)
    settings = pipeline.settings.model_copy(
        update={
            "public_base_url": ORIGIN,
            "cors_origins": ORIGIN,
            "privacy_policy_version": "2026-08-10",
        }
    )
    pipeline.settings = settings
    return TestClient(create_app(settings, pipeline=pipeline)), store


def test_trial_start_sets_365_day_private_cookie_and_restores_it(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)

    started = client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )
    restored = client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={},
    )

    assert started.status_code == 200
    assert started.json()["quota"]["remaining_total"] == 5
    cookie = started.headers["set-cookie"]
    assert "weiquan_trial=" in cookie
    assert "Max-Age=31536000" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert restored.status_code == 200
    assert "set-cookie" not in restored.headers
    assert restored.json()["identity_id"] == started.json()["identity_id"]


def test_trial_consults_five_times_without_saving_content(
    tmp_path: Path,
) -> None:
    client, store = _client(tmp_path)
    started = client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={
            "captcha_token": "captcha-ok",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )
    assert started.status_code == 200

    responses = [
        client.post(
            "/api/trial/consult",
            headers={"Origin": ORIGIN},
            json={"message": f"房东不退押金，第 {index} 次咨询"},
        )
        for index in range(1, 7)
    ]

    assert [response.status_code for response in responses] == [
        200,
        200,
        200,
        200,
        200,
        429,
    ]
    assert [
        response.json()["quota"]["remaining_total"]
        for response in responses[:5]
    ] == [4, 3, 2, 1, 0]
    assert responses[5].json()["detail"]["code"] == (
        "trial_quota_exceeded"
    )
    assert store.list_sessions() == []


def test_trial_followup_reuses_context_without_saving_to_database(
    tmp_path: Path,
) -> None:
    client, database_store = _client(tmp_path)
    started = client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={
            "captcha_token": "captcha-ok",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )
    first_message = (
        "我把游戏账号借给网友了，里面充值了4000元，"
        "他用我的账号开挂导致账号被封了十年"
    )
    second_message = (
        "我联系不上人，他把我删除了我该怎么办，"
        "能在法院直接起诉他吗"
    )

    first = client.post(
        "/api/trial/consult",
        headers={"Origin": ORIGIN},
        json={"message": first_message},
    )
    second = client.post(
        "/api/trial/consult",
        headers={"Origin": ORIGIN},
        json={
            "session_id": first.json()["session_id"],
            "message": second_message,
        },
    )

    assert started.status_code == 200
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["session_id"] == first.json()["session_id"]
    assert first.json()["quota"]["remaining_total"] == 4
    assert second.json()["quota"]["remaining_total"] == 3
    provider = client.app.state.consultation_pipeline.provider
    assert provider.extraction_calls == 2
    context = provider.extraction_context_calls[1]
    assert context["is_followup"] is True
    assert context["confirmed_facts"] == {"amount": 4000.0}
    assert context["recent_conversation"][0]["user_message"] == (
        first_message
    )
    assert context["recent_conversation"][0]["assistant_reply"]
    assert database_store.list_sessions() == []


def test_trial_session_cannot_be_reused_by_another_identity(
    tmp_path: Path,
) -> None:
    first_client, _ = _client(tmp_path)
    first_client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={
            "captcha_token": "captcha-ok",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )
    first = first_client.post(
        "/api/trial/consult",
        headers={"Origin": ORIGIN},
        json={"message": "房东拒绝退还我的两千元押金"},
    )

    with TestClient(first_client.app) as second_client:
        second_started = second_client.post(
            "/api/trial/start",
            headers={"Origin": ORIGIN},
            json={
                "captcha_token": "captcha-ok",
                "privacy_version": "2026-08-10",
                "privacy_accepted": True,
            },
        )
        stolen = second_client.post(
            "/api/trial/consult",
            headers={"Origin": ORIGIN},
            json={
                "session_id": first.json()["session_id"],
                "message": "继续回答这个案件",
            },
        )

    assert first.status_code == 200
    assert second_started.status_code == 200
    assert stolen.status_code == 422
    assert stolen.json()["detail"]["code"] == "session_not_found"


def test_trial_consult_activates_ip_grant_before_pipeline(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    started = client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={
            "captcha_token": "captcha-ok",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )
    manager = client.app.state.trial_identity_manager
    manager.activate_for_consult = Mock(
        wraps=manager.activate_for_consult
    )

    consulted = client.post(
        "/api/trial/consult",
        headers={"Origin": ORIGIN},
        json={"message": "房东拒绝退还押金"},
    )

    assert started.status_code == 200
    assert consulted.status_code == 200
    manager.activate_for_consult.assert_called_once()
    activated_identity = manager.activate_for_consult.call_args.args[0]
    assert activated_identity.id == started.json()["identity_id"]


def test_trial_requires_valid_cookie_and_limits_text_to_3000(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    missing = client.post(
        "/api/trial/consult",
        headers={"Origin": ORIGIN},
        json={"message": "房东不退押金"},
    )
    client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={
            "captcha_token": "captcha-ok",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )
    too_long = client.post(
        "/api/trial/consult",
        headers={"Origin": ORIGIN},
        json={"message": "诉" * 3001},
    )

    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "trial_identity_required"
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["code"] == "request_validation"


def test_failed_trial_start_does_not_set_cookie(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={
            "captcha_token": "",
            "privacy_version": "2026-08-10",
            "privacy_accepted": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation"
    assert "set-cookie" not in response.headers


def test_trial_without_captcha_still_requires_privacy(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)

    response = client.post(
        "/api/trial/start",
        headers={"Origin": ORIGIN},
        json={},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "privacy_acceptance_required"
    )
    assert "set-cookie" not in response.headers


def test_registered_consult_requires_current_privacy_then_returns_quota(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    settings = pipeline.settings.model_copy(
        update={
            "deployment_mode": "test",
            "rollout_stage": "public",
            "public_base_url": ORIGIN,
            "cors_origins": ORIGIN,
            "privacy_policy_version": "2026-08-10",
        }
    )
    pipeline.settings = settings
    application = create_app(settings, pipeline=pipeline)

    with TestClient(application) as client:
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": ORIGIN},
            json={
                "email": "quota@example.com",
                "password": "password-12345",
                "privacy_version": "2026-08-10",
                "privacy_accepted": True,
            },
        )
        assert registered.status_code == 202
        verification_code = (
            application.state.auth_mailer.verification_messages[0][1]
        )
        assert client.post(
            "/api/auth/verify",
            headers={"Origin": ORIGIN},
            json={
                "email": "quota@example.com",
                "code": verification_code,
            },
        ).status_code == 200
        assert client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={
                "email": "quota@example.com",
                "password": "password-12345",
            },
        ).status_code == 200
        csrf = client.get("/api/auth/csrf").json()["csrf_token"]
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
        }

        blocked = client.post(
            "/api/consult",
            headers=headers,
            json={"message": "房东不退押金"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == (
            "privacy_acceptance_required"
        )

        accepted = client.post(
            "/api/privacy/accept",
            headers=headers,
            json={
                "context": "consultation",
                "policy_version": "2026-08-10",
            },
        )
        consulted = client.post(
            "/api/consult",
            headers=headers,
            json={"message": "房东不退押金"},
        )

        assert accepted.status_code == 200
        assert consulted.status_code == 200
        assert consulted.json()["quota"]["remaining_daily"] == 9
        assert consulted.json()["quota"]["remaining_monthly"] == 49
