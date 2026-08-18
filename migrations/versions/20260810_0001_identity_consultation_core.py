"""Add identity-owned consultation persistence.

Revision ID: 20260810_0001
Revises: 20260810_0000
Create Date: 2026-08-10
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260810_0001"
down_revision: str | None = "20260810_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=False)
    jsonb_type = postgresql.JSONB()

    op.create_table(
        "users",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )

    op.create_table(
        "consultation_sessions",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("owner_id", uuid_type, nullable=False),
        sa.Column("scenario_id", sa.String(length=100)),
        sa.Column(
            "facts",
            jsonb_type,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "followup_round",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("jurisdiction", sa.String(length=100)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "followup_round BETWEEN 0 AND 2",
            name=op.f(
                "ck_consultation_sessions_followup_round_range"
            ),
        ),
        sa.CheckConstraint(
            "status IN ("
            "'collecting', 'need_more_facts', 'ready', 'escalate', 'error'"
            ")",
            name=op.f("ck_consultation_sessions_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ("owner_id",),
            ("users.id",),
            name="fk_consultation_sessions_owner_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_consultation_sessions",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "id",
            name="uq_consultation_sessions_owner_id_id",
        ),
    )
    op.create_index(
        "ix_consultation_sessions_owner_updated",
        "consultation_sessions",
        ["owner_id", "updated_at"],
    )
    op.create_index(
        "ix_consultation_sessions_expires",
        "consultation_sessions",
        ["expires_at"],
    )

    op.create_table(
        "consultation_turns",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("owner_id", uuid_type, nullable=False),
        sa.Column("session_id", uuid_type, nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
        sa.Column(
            "facts",
            jsonb_type,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "rule_matches",
            jsonb_type,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "response",
            jsonb_type,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("provider_name", sa.String(length=50)),
        sa.Column("provider_model", sa.String(length=200)),
        sa.Column(
            "provider_request_id",
            sa.String(length=200),
        ),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("estimated_cost_usd", sa.Numeric(18, 8)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 "
            "AND total_tokens >= 0",
            name=op.f(
                "ck_consultation_turns_token_counts_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name=op.f(
                "ck_consultation_turns_estimated_cost_nonnegative"
            ),
        ),
        sa.ForeignKeyConstraint(
            ("owner_id", "session_id"),
            (
                "consultation_sessions.owner_id",
                "consultation_sessions.id",
            ),
            name=(
                "fk_consultation_turns_owner_id_"
                "consultation_sessions"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_consultation_turns",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "id",
            name="uq_consultation_turns_owner_id_id",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "session_id",
            "id",
            name="uq_consultation_turns_owner_session_id",
        ),
    )
    op.create_index(
        "ix_consultation_turns_owner_session_created",
        "consultation_turns",
        ["owner_id", "session_id", "created_at"],
    )

    op.create_table(
        "consultation_attachments",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("owner_id", uuid_type, nullable=False),
        sa.Column("session_id", uuid_type),
        sa.Column("turn_id", uuid_type),
        sa.Column("turn_position", sa.Integer()),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column(
            "original_name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "media_type",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column(
            "extraction_method",
            sa.String(length=30),
        ),
        sa.Column("confirmed_text", sa.Text()),
        sa.Column(
            "warnings",
            jsonb_type,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("reservation_id", uuid_type),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
        ),
        sa.CheckConstraint(
            "status IN ("
            "'processing', 'review_required', 'confirmed', "
            "'failed', 'bound'"
            ")",
            name=op.f(
                "ck_consultation_attachments_status_allowed"
            ),
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name=op.f(
                "ck_consultation_attachments_size_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count >= 1",
            name=op.f(
                "ck_consultation_attachments_page_count_positive"
            ),
        ),
        sa.CheckConstraint(
            "turn_position IS NULL OR turn_position BETWEEN 0 AND 2",
            name=op.f(
                "ck_consultation_attachments_turn_position_range"
            ),
        ),
        sa.CheckConstraint(
            "(status = 'bound' AND session_id IS NOT NULL "
            "AND turn_id IS NOT NULL AND turn_position IS NOT NULL "
            "AND reservation_id IS NULL AND reserved_at IS NULL "
            "AND expires_at IS NULL) OR "
            "(status <> 'bound' AND session_id IS NULL "
            "AND turn_id IS NULL AND expires_at IS NOT NULL)",
            name=op.f(
                "ck_consultation_attachments_binding_state"
            ),
        ),
        sa.CheckConstraint(
            "(status IN ('confirmed', 'bound') "
            "AND confirmed_text IS NOT NULL "
            "AND length(btrim(confirmed_text)) > 0) OR "
            "(status IN ('processing', 'review_required', 'failed') "
            "AND confirmed_text IS NULL)",
            name=op.f(
                "ck_consultation_attachments_confirmed_text_state"
            ),
        ),
        sa.ForeignKeyConstraint(
            ("owner_id", "session_id"),
            (
                "consultation_sessions.owner_id",
                "consultation_sessions.id",
            ),
            name=(
                "fk_consultation_attachments_owner_id_"
                "consultation_sessions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("owner_id", "session_id", "turn_id"),
            (
                "consultation_turns.owner_id",
                "consultation_turns.session_id",
                "consultation_turns.id",
            ),
            name=(
                "fk_consultation_attachments_owner_id_"
                "consultation_turns"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_consultation_attachments",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "id",
            name="uq_consultation_attachments_owner_id_id",
        ),
    )
    op.create_index(
        "ix_consultation_attachments_owner_expires",
        "consultation_attachments",
        ["owner_id", "expires_at"],
    )
    op.create_index(
        "ix_consultation_attachments_owner_turn",
        "consultation_attachments",
        ["owner_id", "turn_id", "turn_position"],
    )
    op.create_index(
        "uq_consultation_attachments_owner_reservation_position",
        "consultation_attachments",
        ["owner_id", "reservation_id", "turn_position"],
        unique=True,
        postgresql_where=sa.text("reservation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_consultation_attachments_owner_turn_position",
        "consultation_attachments",
        ["owner_id", "turn_id", "turn_position"],
        unique=True,
        postgresql_where=sa.text("turn_id IS NOT NULL"),
    )

    op.create_table(
        "content_audit_records",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("owner_id", uuid_type, nullable=False),
        sa.Column("audit_id", uuid_type, nullable=False),
        sa.Column("session_id", uuid_type, nullable=False),
        sa.Column("turn_id", uuid_type),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("playbook_id", sa.String(length=100)),
        sa.Column(
            "playbook_version",
            sa.String(length=50),
        ),
        sa.Column(
            "citations",
            jsonb_type,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_category", sa.String(length=100)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('started', 'ok', 'error', 'degraded')",
            name=op.f("ck_content_audit_records_status_allowed"),
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name=op.f(
                "ck_content_audit_records_duration_nonnegative"
            ),
        ),
        sa.ForeignKeyConstraint(
            ("owner_id", "session_id"),
            (
                "consultation_sessions.owner_id",
                "consultation_sessions.id",
            ),
            name=(
                "fk_content_audit_records_owner_id_"
                "consultation_sessions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("owner_id", "session_id", "turn_id"),
            (
                "consultation_turns.owner_id",
                "consultation_turns.session_id",
                "consultation_turns.id",
            ),
            name=(
                "fk_content_audit_records_owner_id_"
                "consultation_turns"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_content_audit_records",
        ),
    )
    op.create_index(
        "ix_content_audit_records_owner_session",
        "content_audit_records",
        ["owner_id", "session_id", "created_at"],
    )
    op.create_index(
        "ix_content_audit_records_owner_audit",
        "content_audit_records",
        ["owner_id", "audit_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("content_audit_records")
    op.drop_table("consultation_attachments")
    op.drop_table("consultation_turns")
    op.drop_table("consultation_sessions")
    op.drop_table("users")
