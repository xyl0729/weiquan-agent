from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException, Request, status

from app.config import Settings


class CircuitState(Protocol):
    def is_tripped(self) -> bool: ...


class DailyRateState(Protocol):
    def exceeded(self, client_ip: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class Credential:
    key: str
    source: str
    metered: bool


def resolve_credential(
    *,
    user_key: str | None,
    client_ip: str,
    settings: Settings,
    circuit: CircuitState,
    ratelimit: DailyRateState,
) -> Credential:
    normalized_user_key = (user_key or "").strip()
    if normalized_user_key:
        return Credential(
            key=normalized_user_key,
            source="user",
            metered=False,
        )

    if settings.key_mode == "byok":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先填写你的 API Key",
        )

    if circuit.is_tripped():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="今日免费额度已用完，可填写自己的 API Key 继续使用",
        )

    if ratelimit.exceeded(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="今日免费次数已用完，可填写自己的 API Key 继续使用",
        )

    if settings.server_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="服务端未配置 API Key",
        )

    return Credential(
        key=settings.server_api_key.get_secret_value(),
        source="server",
        metered=True,
    )


def request_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

