from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from argon2.low_level import Type


class PasswordPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    valid: bool
    upgraded_hash: str | None = None


class PasswordManager:
    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher(type=Type.ID)

    def validate(self, password: str) -> None:
        if not 8 <= len(password) <= 128:
            raise PasswordPolicyError(
                "密码长度必须为 8 至 128 个字符"
            )

    def hash(self, password: str) -> str:
        self.validate(password)
        return self._hasher.hash(password)

    def verify(
        self,
        password: str,
        encoded_hash: str,
    ) -> PasswordVerification:
        try:
            valid = self._hasher.verify(encoded_hash, password)
        except (
            InvalidHashError,
            VerificationError,
            VerifyMismatchError,
            ValueError,
        ):
            return PasswordVerification(valid=False)
        if not valid:
            return PasswordVerification(valid=False)
        upgraded = None
        if self._hasher.check_needs_rehash(encoded_hash):
            upgraded = self._hasher.hash(password)
        return PasswordVerification(
            valid=True,
            upgraded_hash=upgraded,
        )
