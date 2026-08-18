from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.auth.errors import AuthError


class TrialIdentityLimitError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "trial_identity_limit_exceeded",
            "当前网络环境的试用领取次数已达上限",
            status_code=429,
        )


class TrialIdentityRequiredError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "trial_identity_required",
            "请先开始试用",
            status_code=401,
        )


class TrialIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    token_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ip_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=100)
    created_at: datetime
    expires_at: datetime


class TrialStartResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: TrialIdentity
    cookie_value: str | None = None
    created: bool
