from __future__ import annotations

from app.auth.errors import AuthError


class AdminRequiredError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "admin_required",
            "需要管理员权限",
            status_code=403,
        )

