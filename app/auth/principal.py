from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from app.auth.models import AuthContext, UserRole
from app.db.contracts import LOCAL_DEVELOPMENT_OWNER_ID


@dataclass(frozen=True, slots=True)
class RegisteredPrincipal:
    user_id: str
    session_id: str
    role: UserRole

    @classmethod
    def from_context(
        cls,
        context: AuthContext,
    ) -> "RegisteredPrincipal":
        return cls(
            user_id=context.user.id,
            session_id=context.session.id,
            role=context.user.role,
        )


@dataclass(frozen=True, slots=True)
class LocalPrincipal:
    user_id: str = LOCAL_DEVELOPMENT_OWNER_ID
    session_id: None = None
    role: Literal["local"] = "local"


Principal: TypeAlias = RegisteredPrincipal | LocalPrincipal

