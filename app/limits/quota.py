from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.errors import SafeApplicationError


QuotaKind = Literal["trial", "registered"]
ReservationStatus = Literal["reserved", "succeeded", "refunded"]


class QuotaExceededError(SafeApplicationError):
    def __init__(self, code: str) -> None:
        messages = {
            "trial_quota_exceeded": "试用次数已用完",
            "trial_daily_capacity_exceeded": "今日试用服务已达上限",
            "registered_daily_quota_exceeded": "今日咨询次数已用完",
            "registered_monthly_quota_exceeded": "本月咨询次数已用完",
        }
        if code not in messages:
            raise ValueError("未知配额错误")
        super().__init__(code, messages[code])


class QuotaBucketSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket_type: str = Field(min_length=1, max_length=50)
    subject_key: str = Field(min_length=1, max_length=100)
    period_key: str = Field(min_length=1, max_length=20)
    limit: int = Field(ge=1)
    exceeded_code: str = Field(min_length=1, max_length=100)
    resets_at: datetime | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.bucket_type, self.subject_key, self.period_key


class QuotaReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=36, max_length=36)
    kind: QuotaKind
    subject_id: str = Field(min_length=1, max_length=100)
    logical_call_id: str = Field(min_length=1, max_length=100)
    status: ReservationStatus
    bucket_keys: tuple[tuple[str, str, str], ...]
    created_at: datetime
    updated_at: datetime


class TrialQuotaStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    remaining_total: int = Field(ge=0, le=5)


class RegisteredQuotaStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    remaining_daily: int = Field(ge=0, le=10)
    remaining_monthly: int = Field(ge=0, le=50)
    day_resets_at: datetime
    month_resets_at: datetime

