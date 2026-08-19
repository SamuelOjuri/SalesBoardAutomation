"""Make processing job input identity comparison valid for PostgreSQL JSON.

Revision ID: 0002_fix_job_identity_trigger
Revises: 0001_backend_foundation
Create Date: 2026-08-19
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0002_fix_job_identity_trigger"
down_revision: str | None = "0001_backend_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_processing_job_input_identity_change()
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


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_processing_job_input_identity_change()
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
