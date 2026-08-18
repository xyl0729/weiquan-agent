"""Add consultation soft deletion and content-free recovery outbox.

Revision ID: 20260810_0006
Revises: 20260810_0005
Create Date: 2026-08-10
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260810_0006"
down_revision: str | None = "20260810_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=False)
    op.add_column(
        "consultation_sessions",
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_consultation_sessions_deleted_expires",
        "consultation_sessions",
        ["deleted_at", "expires_at"],
    )

    op.create_table(
        "consultation_deletion_outbox",
        sa.Column("session_id", uuid_type, nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "manifest_uploaded_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_attempted_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column(
            "last_error_category",
            sa.String(length=50),
        ),
        sa.CheckConstraint(
            "manifest_uploaded_at IS NULL "
            "OR manifest_uploaded_at >= deleted_at",
            name=op.f(
                "ck_consultation_deletion_outbox_"
                "manifest_upload_after_deletion"
            ),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR ("
            "manifest_uploaded_at IS NOT NULL "
            "AND completed_at >= deleted_at"
            ")",
            name=op.f(
                "ck_consultation_deletion_outbox_"
                "completion_after_manifest"
            ),
        ),
        sa.CheckConstraint(
            "last_error_category IS NULL OR last_error_category IN ("
            "'encryption_failed', 'upload_failed', 'storage_failed'"
            ")",
            name=op.f(
                "ck_consultation_deletion_outbox_"
                "error_category_allowed"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "session_id",
            name="pk_consultation_deletion_outbox",
        ),
    )
    op.create_index(
        "ix_consultation_deletion_outbox_completed",
        "consultation_deletion_outbox",
        ["completed_at", "deleted_at"],
    )


def downgrade() -> None:
    op.drop_table("consultation_deletion_outbox")
    op.drop_index(
        "ix_consultation_sessions_deleted_expires",
        table_name="consultation_sessions",
    )
    op.drop_column("consultation_sessions", "deleted_at")
