from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any


SAFE_LOG_FIELDS = frozenset(
    {
        "event",
        "request_id",
        "identity_digest",
        "route",
        "method",
        "status_code",
        "duration_ms",
        "response_bytes",
        "category",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reservation_id",
        "quota_status",
        "ocr_pages",
    }
)

_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
_HEX_32_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ROUTE_PATTERN = re.compile(r"^/[A-Za-z0-9_./{}:-]{0,255}$")
_METHOD_PATTERN = re.compile(r"^[A-Z]{1,12}$")
_SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_INTEGER_FIELDS = {
    "status_code",
    "duration_ms",
    "response_bytes",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "ocr_pages",
}


class SafeJsonFormatter(logging.Formatter):
    """Serialize only explicitly approved, non-content log metadata."""

    def format(self, record: logging.LogRecord) -> str:
        event = _safe_event(record)
        payload: dict[str, Any] = {"event": event}
        for field in SAFE_LOG_FIELDS - {"event"}:
            value = _safe_value(field, record.__dict__.get(field))
            if value is not None:
                payload[field] = value
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


def configure_safe_json_logging(
    log_dir: Path,
    *,
    filename: str = "app.jsonl",
) -> logging.Logger:
    directory = Path(log_dir).resolve()
    normalized_filename = filename.strip()
    if (
        not normalized_filename
        or Path(normalized_filename).name != normalized_filename
        or not normalized_filename.endswith(".jsonl")
    ):
        raise ValueError("日志文件名无效")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass

    target = directory / normalized_filename
    logger = logging.getLogger("weiquan")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    resolved_target = os.path.normcase(str(target))
    for handler in logger.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and os.path.normcase(handler.baseFilename) == resolved_target
        ):
            return logging.getLogger("weiquan.request")

    handler = logging.FileHandler(
        target,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(SafeJsonFormatter())
    logger.addHandler(handler)
    return logging.getLogger("weiquan.request")


def _safe_event(record: logging.LogRecord) -> str:
    if (
        isinstance(record.msg, str)
        and not record.args
        and _EVENT_PATTERN.fullmatch(record.msg)
    ):
        return record.msg
    return "application.event"


def _safe_value(field: str, value: object) -> object | None:
    if value is None:
        return None
    if field in _INTEGER_FIELDS:
        if isinstance(value, bool):
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return normalized if normalized >= 0 else None
    if not isinstance(value, str):
        return None
    if field == "request_id":
        return value if _HEX_32_PATTERN.fullmatch(value) else None
    if field == "identity_digest":
        return value if _HEX_64_PATTERN.fullmatch(value) else None
    if field == "route":
        return value if _ROUTE_PATTERN.fullmatch(value) else None
    if field == "method":
        return value if _METHOD_PATTERN.fullmatch(value) else None
    if field in {"category", "provider", "model", "quota_status"}:
        return value if _SAFE_TOKEN_PATTERN.fullmatch(value) else None
    if field == "reservation_id":
        return value if _SAFE_TOKEN_PATTERN.fullmatch(value) else None
    return None
