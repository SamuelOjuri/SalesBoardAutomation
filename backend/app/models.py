"""Durable webhook, processing, job, and audit records."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WebhookEventStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class ProcessingItemState(StrEnum):
    WAITING_FOR_EMAIL = "waiting_for_email"
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    ANALYZED = "analyzed"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    INELIGIBLE = "ineligible"
    FAILED = "failed"


class ProcessingJobStatus(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingJobStage(StrEnum):
    WAITING_FOR_EMAIL = "waiting_for_email"
    EXTRACTING = "extracting"
    MATCHING_ACCOUNT = "matching_account"
    VALIDATING = "validating"
    PUBLISHING = "publishing"


ACTIVE_JOB_STATUSES = (
    ProcessingJobStatus.SCHEDULED.value,
    ProcessingJobStatus.RUNNING.value,
    ProcessingJobStatus.RETRY_WAIT.value,
)


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_sql_values(tuple(WebhookEventStatus))})",
            name="ck_webhook_events_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_webhook_events_attempt_count",
        ),
        Index("ix_webhook_events_board_item", "board_id", "item_id"),
        Index("ix_webhook_events_status_received", "status", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    monday_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    board_id: Mapped[str | None] = mapped_column(String, nullable=True)
    item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    group_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str | None] = mapped_column(String, nullable=True)
    column_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    authenticated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=WebhookEventStatus.RECEIVED.value,
        server_default=WebhookEventStatus.RECEIVED.value,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    audits: Mapped[list[ProcessingAudit]] = relationship(
        back_populates="webhook_event"
    )


class ProcessingItem(Base):
    __tablename__ = "processing_items"
    __table_args__ = (
        UniqueConstraint(
            "board_id", "item_id", name="uq_processing_items_board_item"
        ),
        CheckConstraint(
            "((latest_input_revision IS NULL AND latest_pipeline_version IS NULL) OR "
            "(latest_input_revision IS NOT NULL AND latest_pipeline_version IS NOT NULL))",
            name="ck_processing_items_desired_identity_pair",
        ),
        CheckConstraint(
            f"state IN ({_sql_values(tuple(ProcessingItemState))})",
            name="ck_processing_items_state",
        ),
        Index("ix_processing_items_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    board_id: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str] = mapped_column(String, nullable=False)
    latest_input_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_pipeline_version: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=ProcessingItemState.WAITING_FOR_EMAIL.value,
        server_default=ProcessingItemState.WAITING_FOR_EMAIL.value,
    )
    postcode_result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    account_match_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    warnings_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    supersession_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    audits: Mapped[list[ProcessingAudit]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["processing_items.board_id", "processing_items.item_id"],
            name="fk_processing_jobs_item",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(tuple(ProcessingJobStatus))})",
            name="ck_processing_jobs_status",
        ),
        CheckConstraint(
            "stage IS NULL OR "
            f"stage IN ({_sql_values(tuple(ProcessingJobStage))})",
            name="ck_processing_jobs_stage",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_processing_jobs_attempt_counts",
        ),
        Index(
            "ix_processing_jobs_status_scheduled_for", "status", "scheduled_for"
        ),
        Index("ix_processing_jobs_board_item", "board_id", "item_id"),
        Index("ix_processing_jobs_status_heartbeat", "status", "heartbeat_at"),
        Index(
            "uq_processing_jobs_active_item",
            "board_id",
            "item_id",
            unique=True,
            postgresql_where=text(
                f"status IN ({_sql_values(ACTIVE_JOB_STATUSES)})"
            ),
            sqlite_where=text(f"status IN ({_sql_values(ACTIVE_JOB_STATUSES)})"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    board_id: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str] = mapped_column(String, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String, nullable=False)
    input_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest_json: Mapped[list[dict[str, str | int]]] = mapped_column(
        JSON, nullable=False
    )
    pipeline_version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=ProcessingJobStatus.SCHEDULED.value,
        server_default=ProcessingJobStatus.SCHEDULED.value,
    )
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by_revision: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    item: Mapped[ProcessingItem] = relationship(back_populates="jobs")
    audits: Mapped[list[ProcessingAudit]] = relationship(back_populates="job")


class ProcessingAudit(Base):
    __tablename__ = "processing_audits"
    __table_args__ = (
        ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["processing_items.board_id", "processing_items.item_id"],
            name="fk_processing_audits_item",
            ondelete="CASCADE",
        ),
        Index("ix_processing_audits_board_item", "board_id", "item_id"),
        Index("ix_processing_audits_job", "job_id"),
        Index("ix_processing_audits_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    board_id: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str] = mapped_column(String, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    webhook_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("webhook_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    input_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(String, nullable=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    item: Mapped[ProcessingItem] = relationship(back_populates="audits")
    job: Mapped[ProcessingJob | None] = relationship(back_populates="audits")
    webhook_event: Mapped[WebhookEvent | None] = relationship(back_populates="audits")


@event.listens_for(ProcessingJob, "before_update")
def _prevent_job_input_identity_change(
    _mapper: object, _connection: object, target: ProcessingJob
) -> None:
    state = inspect(target)
    immutable_fields = ("input_revision", "input_manifest_json", "pipeline_version")
    changed_fields = [
        field_name
        for field_name in immutable_fields
        if state.attrs[field_name].history.has_changes()
    ]
    if changed_fields:
        raise ValueError(
            "processing job input identity is immutable: " + ", ".join(changed_fields)
        )