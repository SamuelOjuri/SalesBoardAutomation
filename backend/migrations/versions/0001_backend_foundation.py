"""Add the Sales board processing foundation.

Revision ID: 0001_backend_foundation
Revises:
Create Date: 2026-08-19
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_backend_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WEBHOOK_EVENT_STATUSES = ("received", "processing", "processed", "failed")
PROCESSING_ITEM_STATES = (
    "waiting_for_email",
    "scheduled",
    "processing",
    "analyzed",
    "publishing",
    "completed",
    "ineligible",
    "failed",
)
PROCESSING_JOB_STATUSES = (
    "scheduled",
    "running",
    "retry_wait",
    "completed",
    "failed",
    "cancelled",
)
ACTIVE_JOB_STATUSES = ("scheduled", "running", "retry_wait")
PROCESSING_JOB_STAGES = (
    "waiting_for_email",
    "extracting",
    "matching_account",
    "validating",
    "publishing",
)


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("monday_event_id", sa.String(), nullable=True),
        sa.Column("subscription_id", sa.String(), nullable=True),
        sa.Column("trigger_uuid", sa.String(), nullable=True),
        sa.Column("board_id", sa.String(), nullable=True),
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column("group_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=True),
        sa.Column("column_id", sa.String(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "authenticated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_webhook_events_idempotency_key"
        ),
        sa.CheckConstraint(
            f"status IN ({_sql_values(WEBHOOK_EVENT_STATUSES)})",
            name="ck_webhook_events_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_webhook_events_attempt_count"
        ),
    )
    op.create_index(
        "ix_webhook_events_board_item", "webhook_events", ["board_id", "item_id"]
    )
    op.create_index(
        "ix_webhook_events_status_received",
        "webhook_events",
        ["status", "received_at"],
    )

    op.create_table(
        "processing_items",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("latest_input_revision", sa.String(length=64), nullable=True),
        sa.Column("latest_pipeline_version", sa.String(), nullable=True),
        sa.Column(
            "state",
            sa.String(),
            nullable=False,
            server_default=sa.text("'waiting_for_email'"),
        ),
        sa.Column("postcode_result_json", sa.JSON(), nullable=True),
        sa.Column("account_match_json", sa.JSON(), nullable=True),
        sa.Column(
            "warnings_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("supersession_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "board_id", "item_id", name="uq_processing_items_board_item"
        ),
        sa.CheckConstraint(
            "((latest_input_revision IS NULL AND latest_pipeline_version IS NULL) OR "
            "(latest_input_revision IS NOT NULL AND latest_pipeline_version IS NOT NULL))",
            name="ck_processing_items_desired_identity_pair",
        ),
        sa.CheckConstraint(
            f"state IN ({_sql_values(PROCESSING_ITEM_STATES)})",
            name="ck_processing_items_state",
        ),
    )
    op.create_index(
        "ix_processing_items_state", "processing_items", ["state"]
    )

    op.create_table(
        "processing_jobs",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("trigger_type", sa.String(), nullable=False),
        sa.Column("input_revision", sa.String(length=64), nullable=False),
        sa.Column("input_manifest_json", sa.JSON(), nullable=False),
        sa.Column("pipeline_version", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'scheduled'"),
        ),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_revision", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["processing_items.board_id", "processing_items.item_id"],
            name="fk_processing_jobs_item",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"status IN ({_sql_values(PROCESSING_JOB_STATUSES)})",
            name="ck_processing_jobs_status",
        ),
        sa.CheckConstraint(
            f"stage IS NULL OR stage IN ({_sql_values(PROCESSING_JOB_STAGES)})",
            name="ck_processing_jobs_stage",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_processing_jobs_attempt_counts",
        ),
    )
    op.create_index(
        "ix_processing_jobs_status_scheduled_for",
        "processing_jobs",
        ["status", "scheduled_for"],
    )
    op.create_index(
        "ix_processing_jobs_board_item",
        "processing_jobs",
        ["board_id", "item_id"],
    )
    op.create_index(
        "ix_processing_jobs_status_heartbeat",
        "processing_jobs",
        ["status", "heartbeat_at"],
    )
    op.create_index(
        "uq_processing_jobs_active_item",
        "processing_jobs",
        ["board_id", "item_id"],
        unique=True,
        postgresql_where=sa.text(
            f"status IN ({_sql_values(ACTIVE_JOB_STATUSES)})"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION prevent_processing_job_input_identity_change()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.input_revision IS DISTINCT FROM OLD.input_revision OR
               NEW.input_manifest_json::jsonb IS DISTINCT FROM
                   OLD.input_manifest_json::jsonb OR
               NEW.pipeline_version IS DISTINCT FROM OLD.pipeline_version THEN
                RAISE EXCEPTION 'processing job input identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_processing_jobs_input_identity_immutable
        BEFORE UPDATE ON processing_jobs
        FOR EACH ROW
        EXECUTE FUNCTION prevent_processing_job_input_identity_change()
        """
    )

    op.create_table(
        "processing_audits",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("webhook_event_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("input_revision", sa.String(length=64), nullable=True),
        sa.Column("pipeline_version", sa.String(), nullable=True),
        sa.Column(
            "details_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["processing_items.board_id", "processing_items.item_id"],
            name="fk_processing_audits_item",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            name="fk_processing_audits_job",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["webhook_event_id"],
            ["webhook_events.id"],
            name="fk_processing_audits_webhook_event",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_processing_audits_board_item",
        "processing_audits",
        ["board_id", "item_id"],
    )
    op.create_index(
        "ix_processing_audits_job", "processing_audits", ["job_id"]
    )
    op.create_index(
        "ix_processing_audits_created", "processing_audits", ["created_at"]
    )
    op.execute(
        """
        CREATE FUNCTION prevent_processing_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'processing audits are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_processing_audits_append_only
        BEFORE UPDATE OR DELETE ON processing_audits
        FOR EACH ROW
        EXECUTE FUNCTION prevent_processing_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_processing_audits_append_only ON processing_audits"
    )
    op.execute("DROP FUNCTION prevent_processing_audit_mutation()")
    op.drop_index("ix_processing_audits_created", table_name="processing_audits")
    op.drop_index("ix_processing_audits_job", table_name="processing_audits")
    op.drop_index("ix_processing_audits_board_item", table_name="processing_audits")
    op.drop_table("processing_audits")

    op.execute(
        "DROP TRIGGER trg_processing_jobs_input_identity_immutable ON processing_jobs"
    )
    op.execute("DROP FUNCTION prevent_processing_job_input_identity_change()")
    op.drop_index("uq_processing_jobs_active_item", table_name="processing_jobs")
    op.drop_index(
        "ix_processing_jobs_status_heartbeat", table_name="processing_jobs"
    )
    op.drop_index("ix_processing_jobs_board_item", table_name="processing_jobs")
    op.drop_index(
        "ix_processing_jobs_status_scheduled_for", table_name="processing_jobs"
    )
    op.drop_table("processing_jobs")

    op.drop_index("ix_processing_items_state", table_name="processing_items")
    op.drop_table("processing_items")

    op.drop_index(
        "ix_webhook_events_status_received", table_name="webhook_events"
    )
    op.drop_index("ix_webhook_events_board_item", table_name="webhook_events")
    op.drop_table("webhook_events")
