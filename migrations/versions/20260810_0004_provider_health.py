"""Add content-free provider result health state.

Revision ID: 20260810_0004
Revises: 20260810_0003
Create Date: 2026-08-10
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260810_0004"
down_revision: str | None = "20260810_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=False)
    op.create_table(
        "provider_call_results",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("logical_call_id", uuid_type, nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=50), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
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
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name=op.f(
                "ck_provider_call_results_duration_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "outcome IN ("
            "'success', 'timeout', 'network_error', 'rate_limited', "
            "'server_error', 'invalid_output', 'rejected', "
            "'configuration_error', 'provider_error'"
            ")",
            name=op.f(
                "ck_provider_call_results_outcome_allowed"
            ),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 "
            "AND total_tokens >= 0",
            name=op.f(
                "ck_provider_call_results_token_counts_nonnegative"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_provider_call_results",
        ),
    )
    op.create_index(
        "ix_provider_call_results_provider_occurred",
        "provider_call_results",
        ["provider", "occurred_at"],
    )
    op.create_index(
        "ix_provider_call_results_logical_call",
        "provider_call_results",
        ["logical_call_id"],
    )

    op.create_table(
        "provider_health_states",
        sa.Column(
            "provider",
            sa.String(length=50),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column(
            "consecutive_successes",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("last_result_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sample_count BETWEEN 0 AND 10 "
            "AND failure_count BETWEEN 0 AND sample_count "
            "AND consecutive_successes BETWEEN 0 AND 10 "
            "AND consecutive_failures BETWEEN 0 AND 10",
            name=op.f("ck_provider_health_states_counts_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('unknown', 'healthy', 'degraded')",
            name=op.f("ck_provider_health_states_status_allowed"),
        ),
        sa.PrimaryKeyConstraint(
            "provider",
            name="pk_provider_health_states",
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_health_states")
    op.drop_table("provider_call_results")
