"""Durable intake retry deadlines and shared Monday usage counters."""

from alembic import op
import sqlalchemy as sa

revision = "0005_durable_webhook_intake"
down_revision = "0004_monday_cooldowns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("next_retry_at", sa.DateTime(timezone=True)))
    # The former endpoint relied on Monday redelivery to retry these failures.
    # After durable acknowledgement, duplicates no longer drive retry work.
    op.execute(sa.text("""
        UPDATE webhook_events
        SET status = 'received', attempt_count = 0,
            next_retry_at = CURRENT_TIMESTAMP, processed_at = NULL,
            processing_started_at = NULL
        WHERE status = 'failed' AND authenticated = true
    """))
    op.create_index("ix_webhook_events_retry", "webhook_events", ["status", "next_retry_at"])
    op.create_table(
        "monday_usage",
        sa.Column("operation", sa.String(), primary_key=True),
        sa.Column("metric", sa.String(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("monday_usage")
    op.drop_index("ix_webhook_events_retry", table_name="webhook_events")
    op.drop_column("webhook_events", "next_retry_at")
