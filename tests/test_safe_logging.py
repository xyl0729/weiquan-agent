from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.observability.logging import (
    SAFE_LOG_FIELDS,
    SafeJsonFormatter,
    configure_safe_json_logging,
)
from app.observability.request_context import (
    RequestContextMiddleware,
    identity_log_digest,
    set_request_identity,
)


SECRET_MARKER = "SENSITIVE-LOG-MARKER-9f4a"


def _logger(stream: io.StringIO) -> logging.Logger:
    logger = logging.getLogger(f"test.safe.{id(stream)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeJsonFormatter())
    logger.addHandler(handler)
    return logger


def test_json_formatter_emits_only_the_explicit_allowlist() -> None:
    stream = io.StringIO()
    logger = _logger(stream)

    logger.info(
        "consult.completed",
        extra={
            "request_id": "a" * 32,
            "identity_digest": "b" * 64,
            "route": "/api/consult",
            "method": "POST",
            "status_code": 200,
            "duration_ms": 17,
            "category": "ok",
            "password": SECRET_MARKER,
            "authorization": f"Bearer {SECRET_MARKER}",
            "cookie": SECRET_MARKER,
            "message_body": SECRET_MARKER,
            "model_output": SECRET_MARKER,
            "uploaded_filename": SECRET_MARKER,
            "ocr_text": SECRET_MARKER,
            "path": SECRET_MARKER,
            "secret": SECRET_MARKER,
        },
    )

    encoded = stream.getvalue()
    payload = json.loads(encoded)
    assert set(payload) <= SAFE_LOG_FIELDS
    assert payload["event"] == "consult.completed"
    assert payload["request_id"] == "a" * 32
    assert payload["status_code"] == 200
    assert SECRET_MARKER not in encoded
    assert "authorization" not in encoded.casefold()
    assert "cookie" not in encoded.casefold()


def test_formatter_replaces_unstructured_messages_and_exceptions() -> None:
    stream = io.StringIO()
    logger = _logger(stream)

    try:
        raise RuntimeError(SECRET_MARKER)
    except RuntimeError:
        logger.exception(f"free-form {SECRET_MARKER}")

    encoded = stream.getvalue()
    payload = json.loads(encoded)
    assert payload["event"] == "application.event"
    assert SECRET_MARKER not in encoded
    assert "traceback" not in encoded.casefold()


def test_request_context_ignores_supplied_ids_and_hashes_identity() -> None:
    stream = io.StringIO()
    logger = _logger(stream)
    secret = b"request-log-identity-secret-value-32"
    application = FastAPI()
    application.add_middleware(
        RequestContextMiddleware,
        identity_secret=secret,
        logger=logger,
    )

    @application.get("/probe")
    def probe() -> dict[str, bool]:
        set_request_identity("user", "user-id-do-not-log")
        return {"ok": True}

    supplied = "attacker-controlled-request-id"
    response = TestClient(application).get(
        "/probe",
        headers={
            "X-Request-ID": supplied,
            "Authorization": f"Bearer {SECRET_MARKER}",
            "Cookie": f"session={SECRET_MARKER}",
        },
    )

    request_id = response.headers["x-request-id"]
    payload = json.loads(stream.getvalue())
    assert response.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert request_id != supplied
    assert payload["request_id"] == request_id
    assert payload["identity_digest"] == identity_log_digest(
        "user",
        "user-id-do-not-log",
        secret,
    )
    assert "user-id-do-not-log" not in stream.getvalue()
    assert SECRET_MARKER not in stream.getvalue()


def test_identity_digest_is_scoped_and_requires_an_independent_secret() -> None:
    secret = b"request-log-identity-secret-value-32"
    user = identity_log_digest("user", "same-id", secret)
    trial = identity_log_digest("trial", "same-id", secret)

    assert re.fullmatch(r"[0-9a-f]{64}", user)
    assert user != trial
    assert user != identity_log_digest(
        "user",
        "same-id",
        b"another-request-log-secret-value-32",
    )


def test_production_logger_writes_only_safe_json(
    tmp_path: Path,
) -> None:
    logger = configure_safe_json_logging(
        tmp_path,
        filename="test-app.jsonl",
    )
    logger.info(
        "request.completed",
        extra={
            "request_id": "c" * 32,
            "status_code": 200,
            "password": SECRET_MARKER,
        },
    )
    for handler in logging.getLogger("weiquan").handlers:
        handler.flush()

    encoded = (tmp_path / "test-app.jsonl").read_text(
        encoding="utf-8"
    )
    assert json.loads(encoded)["request_id"] == "c" * 32
    assert SECRET_MARKER not in encoded
