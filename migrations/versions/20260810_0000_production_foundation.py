"""Establish the production migration baseline.

Revision ID: 20260810_0000
Revises:
Create Date: 2026-08-10
"""

from typing import Sequence


revision: str = "20260810_0000"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
