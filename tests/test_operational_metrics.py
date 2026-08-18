from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.observability.metrics import (
    OperationalMetrics,
    attachment_temp_snapshot,
)
from tests.test_pipeline import make_pipeline


def test_operational_metrics_are_bounded_content_free_and_windowed() -> None:
    metrics = OperationalMetrics(max_events_per_component=10)
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)

    metrics.record(
        "mail",
        "failure",
        occurred_at=now - timedelta(hours=1),
    )
    metrics.record("mail", "success", occurred_at=now)
    metrics.record("captcha", "rejected", occurred_at=now)

    mail = metrics.snapshot("mail", now=now)
    captcha = metrics.snapshot("captcha", now=now)

    assert mail.to_dict() == {
        "success": 1,
        "failure": 0,
        "rejected": 0,
        "last_result_at": now.isoformat(),
    }
    assert captcha.rejected == 1
    assert set(mail.to_dict()) == {
        "success",
        "failure",
        "rejected",
        "last_result_at",
    }


def test_attachment_metric_does_not_expose_names_or_paths(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "private-user-file.pdf"
    attachment.write_bytes(b"safe")

    snapshot = attachment_temp_snapshot(
        tmp_path,
        now_epoch=attachment.stat().st_mtime + 61,
    )

    assert snapshot.files == 1
    assert snapshot.bytes == 4
    assert snapshot.oldest_age_seconds == 61
    assert "private-user-file" not in str(snapshot.to_dict())


def test_internal_metrics_api_exposes_only_bounded_aggregates(
    tmp_path: Path,
) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    attachment_dir = tmp_path / "private-attachment-jobs"
    attachment_dir.mkdir()
    private_file = attachment_dir / "claimant-name-private.pdf"
    private_file.write_bytes(b"content-free-counting")
    settings = pipeline.settings.model_copy(
        update={"attachment_temp_dir": attachment_dir}
    )
    application = create_app(settings, pipeline=pipeline)
    application.state.operational_metrics.record("mail", "success")
    application.state.operational_metrics.record("captcha", "rejected")
    client = TestClient(application)

    response = client.get("/internal/metrics")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "queue",
        "provider",
        "mail",
        "captcha",
        "attachment",
    }
    assert set(body["queue"]) == {"ocr", "deepseek"}
    assert body["queue"]["ocr"]["max_concurrency"] == (
        settings.ocr_max_concurrency
    )
    assert body["queue"]["deepseek"]["max_waiting"] == (
        settings.deepseek_max_waiting
    )
    assert body["provider"]["status"] == "unknown"
    assert body["mail"]["success"] == 1
    assert body["captcha"]["rejected"] == 1
    assert body["attachment"]["files"] == 1
    assert body["attachment"]["bytes"] == len(b"content-free-counting")
    assert "claimant-name-private" not in response.text
    assert str(attachment_dir) not in response.text
    assert "/internal/metrics" not in client.get("/openapi.json").text
