"""Coordinate Monday retry deadlines across application processes.

Revision ID: 0004_monday_cooldowns
Revises: 0003_shared_reference_indexes
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_monday_cooldowns"
down_revision: str | None = "0003_shared_reference_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monday_cooldowns",
        sa.Column("source_scope", sa.String(64), primary_key=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("monday_cooldowns")
