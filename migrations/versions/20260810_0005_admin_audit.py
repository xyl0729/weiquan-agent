"""Add content-free administrative audit events.

Revision ID: 20260810_0005
Revises: 20260810_0004
Create Date: 2026-08-10
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260810_0005"
down_revision: str | None = "20260810_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=False)
    op.create_table(
        "admin_audit_events",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("admin_id", uuid_type, nullable=False),
        sa.Column("target_user_id", uuid_type, nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "action IN ('revoke_sessions', 'disable_user')",
            name=op.f("ck_admin_audit_events_action_allowed"),
        ),
        sa.CheckConstraint(
            "result IN ('succeeded', 'not_found')",
            name=op.f("ck_admin_audit_events_result_allowed"),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_admin_audit_events",
        ),
    )
    op.create_index(
        "ix_admin_audit_events_occurred",
        "admin_audit_events",
        ["occurred_at"],
    )
    op.create_index(
        "ix_admin_audit_events_admin_occurred",
        "admin_audit_events",
        ["admin_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("admin_audit_events")
