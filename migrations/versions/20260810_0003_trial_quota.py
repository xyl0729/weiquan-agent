"""Add anonymous trial identities and atomic quota reservations.

Revision ID: 20260810_0003
Revises: 20260810_0002
Create Date: 2026-08-10
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260810_0003"
down_revision: str | None = "20260810_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=False)

    op.create_table(
        "trial_identities",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "token_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "ip_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "policy_version",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f(
                "ck_trial_identities_expiry_after_creation"
            ),
        ),
        sa.CheckConstraint(
            "length(ip_digest) = 64",
            name=op.f("ck_trial_identities_ip_digest_length"),
        ),
        sa.CheckConstraint(
            "length(token_digest) = 64",
            name=op.f("ck_trial_identities_token_digest_length"),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_trial_identities",
        ),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_trial_identities_token_digest",
        ),
    )
    op.create_index(
        "ix_trial_identities_expires",
        "trial_identities",
        ["expires_at"],
    )
    op.create_index(
        "ix_trial_identities_ip_created",
        "trial_identities",
        ["ip_digest", "created_at"],
    )

    op.create_table(
        "trial_ip_grants",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("identity_id", uuid_type, nullable=False),
        sa.Column(
            "ip_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f(
                "ck_trial_ip_grants_expiry_after_creation"
            ),
        ),
        sa.CheckConstraint(
            "length(ip_digest) = 64",
            name=op.f("ck_trial_ip_grants_ip_digest_length"),
        ),
        sa.ForeignKeyConstraint(
            ("identity_id",),
            ("trial_identities.id",),
            name="fk_trial_ip_grants_identity_id_trial_identities",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_trial_ip_grants",
        ),
        sa.UniqueConstraint(
            "identity_id",
            name="uq_trial_ip_grants_identity_id",
        ),
    )
    op.create_index(
        "ix_trial_ip_grants_ip_expires",
        "trial_ip_grants",
        ["ip_digest", "expires_at"],
    )
    op.create_index(
        "ix_trial_ip_grants_expires",
        "trial_ip_grants",
        ["expires_at"],
    )

    op.create_table(
        "quota_buckets",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "bucket_type",
            sa.String(length=50),
            nullable=False,
        ),
        sa.Column(
            "subject_key",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "period_key",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column(
            "used_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("limit_count", sa.Integer(), nullable=False),
        sa.Column("resets_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "limit_count >= 1",
            name=op.f("ck_quota_buckets_limit_count_positive"),
        ),
        sa.CheckConstraint(
            "used_count >= 0",
            name=op.f(
                "ck_quota_buckets_used_count_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "used_count <= limit_count",
            name=op.f("ck_quota_buckets_used_not_above_limit"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quota_buckets"),
        sa.UniqueConstraint(
            "bucket_type",
            "subject_key",
            "period_key",
            name="uq_quota_buckets_scope_period",
        ),
    )
    op.create_index(
        "ix_quota_buckets_reset",
        "quota_buckets",
        ["resets_at"],
    )

    op.create_table(
        "quota_reservations",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "kind",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "logical_call_id",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
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
        sa.CheckConstraint(
            "kind IN ('trial', 'registered')",
            name=op.f("ck_quota_reservations_kind_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'succeeded', 'refunded')",
            name=op.f("ck_quota_reservations_status_allowed"),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_quota_reservations",
        ),
        sa.UniqueConstraint(
            "kind",
            "subject_id",
            "logical_call_id",
            name="uq_quota_reservations_logical_call",
        ),
    )
    op.create_index(
        "ix_quota_reservations_status_created",
        "quota_reservations",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_quota_reservations_subject_created",
        "quota_reservations",
        ["kind", "subject_id", "created_at"],
    )

    op.create_table(
        "quota_reservation_buckets",
        sa.Column(
            "reservation_id",
            uuid_type,
            nullable=False,
        ),
        sa.Column("bucket_id", uuid_type, nullable=False),
        sa.ForeignKeyConstraint(
            ("bucket_id",),
            ("quota_buckets.id",),
            name=(
                "fk_quota_reservation_buckets_bucket_id_quota_buckets"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("reservation_id",),
            ("quota_reservations.id",),
            name=(
                "fk_quota_reservation_buckets_reservation_id_"
                "quota_reservations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "reservation_id",
            "bucket_id",
            name="pk_quota_reservation_buckets",
        ),
    )
    op.create_index(
        "ix_quota_reservation_buckets_bucket",
        "quota_reservation_buckets",
        ["bucket_id"],
    )


def downgrade() -> None:
    op.drop_table("quota_reservation_buckets")
    op.drop_table("quota_reservations")
    op.drop_table("quota_buckets")
    op.drop_table("trial_ip_grants")
    op.drop_table("trial_identities")
