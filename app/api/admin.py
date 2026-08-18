from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from app.admin.errors import AdminRequiredError
from app.admin.models import AdminActionResult, AdminDiagnostics
from app.admin.service import AdminService
from app.auth.dependencies import (
    require_auth_context,
    require_csrf_context,
)
from app.auth.models import AuthContext
from app.deps import get_admin_service


router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_read_admin(
    context: AuthContext = Depends(require_auth_context),
) -> AuthContext:
    if context.user.role != "admin":
        raise AdminRequiredError()
    return context


def require_write_admin(
    context: AuthContext = Depends(require_csrf_context),
) -> AuthContext:
    if context.user.role != "admin":
        raise AdminRequiredError()
    return context


@router.get("/diagnostics", response_model=AdminDiagnostics)
def diagnostics(
    _context: AuthContext = Depends(require_read_admin),
    service: AdminService = Depends(get_admin_service),
) -> AdminDiagnostics:
    del _context
    return service.diagnostics()


@router.post(
    "/users/{target_user_id}/revoke-sessions",
    response_model=AdminActionResult,
)
def revoke_user_sessions(
    target_user_id: UUID,
    context: AuthContext = Depends(require_write_admin),
    service: AdminService = Depends(get_admin_service),
) -> AdminActionResult:
    return service.revoke_user_sessions(
        admin_id=context.user.id,
        target_user_id=str(target_user_id),
    )


@router.post(
    "/users/{target_user_id}/disable",
    response_model=AdminActionResult,
)
def disable_user(
    target_user_id: UUID,
    context: AuthContext = Depends(require_write_admin),
    service: AdminService = Depends(get_admin_service),
) -> AdminActionResult:
    return service.disable_user(
        admin_id=context.user.id,
        target_user_id=str(target_user_id),
    )

