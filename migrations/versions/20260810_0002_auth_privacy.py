"""Add account authentication, registration capacity, and privacy.

Revision ID: 20260810_0002
Revises: 20260810_0001
Create Date: 2026-08-10
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260810_0002"
down_revision: str | None = "20260810_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=False)

    op.add_column(
        "users",
        sa.Column("email_normalized", sa.String(length=320)),
    )
    op.add_column("users", sa.Column("password_hash", sa.Text()))
    op.add_column(
        "users",
        sa.Column(
            "status",
            sa.String(length=30),
            server_default=sa.text("'local'"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.String(length=20),
            server_default=sa.text("'user'"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column("verified_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "users",
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_users_status_allowed"),
        "users",
        "status IN ("
        "'local', 'pending_verification', 'active', 'disabled', 'deleted'"
        ")",
    )
    op.create_check_constraint(
        op.f("ck_users_role_allowed"),
        "users",
        "role IN ('user', 'admin')",
    )
    op.create_check_constraint(
        op.f("ck_users_account_credentials_state"),
        "users",
        "(status = 'local' AND email_normalized IS NULL "
        "AND password_hash IS NULL) OR "
        "(status IN ('pending_verification', 'active', 'disabled') "
        "AND email_normalized IS NOT NULL AND password_hash IS NOT NULL) "
        "OR (status = 'deleted' AND email_normalized IS NULL "
        "AND password_hash IS NULL)",
    )
    op.create_index(
        "uq_users_email_normalized",
        "users",
        ["email_normalized"],
        unique=True,
    )
    op.create_index(
        "ix_users_status_created",
        "users",
        ["status", "created_at"],
    )

    op.create_table(
        "registration_capacity",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "capacity_limit",
            sa.Integer(),
            server_default=sa.text("100"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id = 1",
            name=op.f("ck_registration_capacity_singleton"),
        ),
        sa.CheckConstraint(
            "capacity_limit >= 1",
            name=op.f(
                "ck_registration_capacity_limit_positive"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_registration_capacity",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO registration_capacity (id, capacity_limit) "
            "VALUES (1, 100)"
        )
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("user_id", uuid_type, nullable=False),
        sa.Column(
            "token_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("csrf_digest", sa.String(length=64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
        ),
        sa.ForeignKeyConstraint(
            ("user_id",),
            ("users.id",),
            name="fk_auth_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_auth_sessions_token_digest",
        ),
    )
    op.create_index(
        "ix_auth_sessions_user_expires",
        "auth_sessions",
        ["user_id", "expires_at"],
    )
    op.create_index(
        "ix_auth_sessions_expires",
        "auth_sessions",
        ["expires_at"],
    )

    op.create_table(
        "auth_tokens",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("user_id", uuid_type, nullable=False),
        sa.Column(
            "purpose",
            sa.String(length=30),
            nullable=False,
        ),
        sa.Column(
            "token_digest",
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
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column(
            "invalidated_at",
            sa.DateTime(timezone=True),
        ),
        sa.CheckConstraint(
            "purpose IN ('email_verification', 'password_reset')",
            name=op.f("ck_auth_tokens_purpose_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ("user_id",),
            ("users.id",),
            name="fk_auth_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_tokens"),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_auth_tokens_token_digest",
        ),
    )
    op.create_index(
        "ix_auth_tokens_user_purpose_expires",
        "auth_tokens",
        ["user_id", "purpose", "expires_at"],
    )
    op.create_index(
        "ix_auth_tokens_expires",
        "auth_tokens",
        ["expires_at"],
    )

    op.create_table(
        "privacy_acceptances",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("user_id", uuid_type, nullable=False),
        sa.Column(
            "context",
            sa.String(length=30),
            nullable=False,
        ),
        sa.Column(
            "policy_version",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "context IN ('registration', 'consultation')",
            name=op.f(
                "ck_privacy_acceptances_context_allowed"
            ),
        ),
        sa.ForeignKeyConstraint(
            ("user_id",),
            ("users.id",),
            name="fk_privacy_acceptances_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_privacy_acceptances",
        ),
        sa.UniqueConstraint(
            "user_id",
            "context",
            "policy_version",
            name="uq_privacy_acceptances_user_context_version",
        ),
    )
    op.create_index(
        "ix_privacy_acceptances_user_context",
        "privacy_acceptances",
        ["user_id", "context"],
    )

    op.create_table(
        "security_rate_limits",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "action",
            sa.String(length=50),
            nullable=False,
        ),
        sa.Column(
            "key_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "request_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "request_count >= 0",
            name=op.f(
                "ck_security_rate_limits_request_count_nonnegative"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_security_rate_limits",
        ),
        sa.UniqueConstraint(
            "action",
            "key_digest",
            "window_started_at",
            name="uq_security_rate_limits_action_key_window",
        ),
    )
    op.create_index(
        "ix_security_rate_limits_expires",
        "security_rate_limits",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("security_rate_limits")
    op.drop_table("privacy_acceptances")
    op.drop_table("auth_tokens")
    op.drop_table("auth_sessions")
    op.drop_table("registration_capacity")
    op.drop_index("ix_users_status_created", table_name="users")
    op.drop_index("uq_users_email_normalized", table_name="users")
    op.drop_constraint(
        op.f("ck_users_account_credentials_state"),
        "users",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_users_role_allowed"),
        "users",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_users_status_allowed"),
        "users",
        type_="check",
    )
    op.drop_column("users", "updated_at")
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "verified_at")
    op.drop_column("users", "role")
    op.drop_column("users", "status")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "email_normalized")
