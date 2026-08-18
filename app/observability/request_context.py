from __future__ import annotations

import hashlib
import hmac
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from starlette.datastructures import MutableHeaders


@dataclass(slots=True)
class _RequestLogContext:
    request_id: str
    identity_secret: bytes
    identity_digest: str | None = None


_REQUEST_CONTEXT: ContextVar[_RequestLogContext | None] = ContextVar(
    "request_log_context",
    default=None,
)


def identity_log_digest(
    identity_kind: str,
    identity_id: str,
    secret: bytes,
) -> str:
    kind = identity_kind.strip().casefold()
    identity = identity_id.strip()
    if not kind or not identity or not secret:
        raise ValueError("日志身份摘要参数不能为空")
    message = (
        b"weiquan-log-identity-v1\x00"
        + kind.encode("utf-8")
        + b"\x00"
        + identity.encode("utf-8")
    )
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def set_request_identity(identity_kind: str, identity_id: str) -> None:
    context = _REQUEST_CONTEXT.get()
    if context is None:
        return
    context.identity_digest = identity_log_digest(
        identity_kind,
        identity_id,
        context.identity_secret,
    )


class RequestContextMiddleware:
    def __init__(
        self,
        app: Any,
        *,
        identity_secret: bytes,
        logger: logging.Logger | None = None,
    ) -> None:
        if len(identity_secret) < 16:
            raise ValueError("日志身份摘要密钥长度不足")
        self.app = app
        self.identity_secret = bytes(identity_secret)
        self.logger = logger or logging.getLogger("weiquan.request")

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        context = _RequestLogContext(
            request_id=uuid4().hex,
            identity_secret=self.identity_secret,
        )
        token = _REQUEST_CONTEXT.set(context)
        started = time.monotonic()
        status_code = 500
        response_bytes = 0
        category = "ok"

        async def send_with_context(message: dict[str, Any]) -> None:
            nonlocal status_code, response_bytes
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = context.request_id
            elif message.get("type") == "http.response.body":
                response_bytes += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
            if status_code >= 500:
                category = "server_error"
            elif status_code >= 400:
                category = "client_error"
        except Exception:
            category = "unhandled_error"
            raise
        finally:
            duration_ms = max(
                0,
                int((time.monotonic() - started) * 1000),
            )
            route = _route_template(scope)
            self.logger.info(
                "request.completed",
                extra={
                    "request_id": context.request_id,
                    "identity_digest": context.identity_digest,
                    "route": route,
                    "method": str(scope.get("method", "")).upper(),
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "response_bytes": response_bytes,
                    "category": category,
                },
            )
            _REQUEST_CONTEXT.reset(token)


def _route_template(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path.startswith("/"):
        return route_path
    path = scope.get("path")
    if isinstance(path, str) and path.startswith("/"):
        return path
    return "/"

