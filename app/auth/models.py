from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


UserStatus = Literal[
    "pending_verification",
    "active",
    "disabled",
    "deleted",
]
UserRole = Literal["user", "admin"]
TokenPurpose = Literal["email_verification", "password_reset"]
PrivacyContext = Literal["registration", "consultation"]
RegistrationStatus = Literal["created", "duplicate", "capacity_full"]


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: str
    email: str
    password_hash: str
    status: UserStatus
    role: UserRole
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None
    disabled_at: datetime | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuthSessionRecord:
    id: str
    user_id: str
    token_digest: str
    csrf_digest: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuthContext:
    user: UserRecord
    session: AuthSessionRecord


@dataclass(frozen=True, slots=True)
class RegistrationDecision:
    status: RegistrationStatus
    user: UserRecord | None = None


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    user: UserRecord
    email_sent: bool
    created: bool


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: UserRecord
    session_token: str
    expires_at: datetime

