"""Persist complete reference indexes and coordinate refreshes across workers.

Revision ID: 0003_shared_reference_indexes
Revises: 0002_fix_job_identity_trigger
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_shared_reference_indexes"
down_revision: str | None = "0002_fix_job_identity_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_index_snapshots",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("index_kind", sa.String(), nullable=False),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "index_kind IN ('accounts', 'contacts')",
            name="ck_reference_index_snapshots_kind",
        ),
        sa.CheckConstraint(
            "(payload_json IS NULL) = (refreshed_at IS NULL)",
            name="ck_reference_index_snapshots_complete",
        ),
    )


def downgrade() -> None:
    op.drop_table("reference_index_snapshots")
