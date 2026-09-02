"""Operator actions and operational metrics for durable processing."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.config import BOARD_CONTRACT
from app.models import (
    ACTIVE_JOB_STATUSES,
    ProcessingAudit,
    ProcessingItem,
    ProcessingItemState,
    ProcessingJob,
    ProcessingJobStatus,
)
from app.services.intake import (
    IntakeQueueResult,
    SalesItemSnapshot,
    is_excluded_sales_group,
    parse_sales_item_snapshot,
    queue_sales_item_snapshot,
)
from app.services.worker import utc_now


class IntakeItemReader(Protocol):
    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]: ...


ReconcileOutcome = Literal["queued", "coalesced", "ineligible"]


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    item_id: str
    outcome: ReconcileOutcome
    job_id: uuid.UUID | None
    input_revision: str | None


@dataclass(frozen=True, slots=True)
class ProcessingMetrics:
    jobs_by_status: dict[str, int]
    jobs_by_stage: dict[str, int]
    items_by_state: dict[str, int]
    oldest_runnable_age_seconds: float | None
    stale_running_jobs: int

    def as_dict(self) -> dict[str, object]:
        return {
            "jobsByStatus": self.jobs_by_status,
            "jobsByStage": self.jobs_by_stage,
            "itemsByState": self.items_by_state,
            "oldestRunnableAgeSeconds": self.oldest_runnable_age_seconds,
            "staleRunningJobs": self.stale_running_jobs,
        }


def enqueue_sales_item(
    session: Session,
    client: IntakeItemReader,
    item_id: str,
    *,
    pipeline_version: str,
    excluded_group_ids: Sequence[str] = (),
    now: datetime | None = None,
) -> IntakeQueueResult:
    snapshot = _authoritative_snapshot(
        client,
        item_id,
        excluded_group_ids=excluded_group_ids,
    )
    if snapshot.board_id != str(BOARD_CONTRACT.sales_board_id):
        raise ValueError("Monday returned a Sales item from the wrong board")
    if is_excluded_sales_group(snapshot.group_id, excluded_group_ids):
        raise ValueError("Sales item belongs to an excluded group")
    if not snapshot.active or not snapshot.email_assets:
        raise ValueError("Sales item has no active supported Email input")
    return queue_sales_item_snapshot(
        session,
        snapshot,
        pipeline_version=pipeline_version,
        trigger_type="operator_enqueue",
        excluded_group_ids=excluded_group_ids,
        now=now,
    )


def reconcile_sales_item(
    session: Session,
    client: IntakeItemReader,
    item_id: str,
    *,
    pipeline_version: str,
    excluded_group_ids: Sequence[str] = (),
    now: datetime | None = None,
) -> ReconcileResult:
    reconciled_at = now or utc_now()
    snapshot = _authoritative_snapshot(
        client,
        item_id,
        excluded_group_ids=excluded_group_ids,
    )
    managed_board_id = str(BOARD_CONTRACT.sales_board_id)
    board_managed = snapshot.board_id == managed_board_id
    group_excluded = is_excluded_sales_group(
        snapshot.group_id,
        excluded_group_ids,
    )
    if (
        board_managed
        and snapshot.active
        and snapshot.email_assets
        and not group_excluded
    ):
        queued = queue_sales_item_snapshot(
            session,
            snapshot,
            pipeline_version=pipeline_version,
            trigger_type="operator_reconcile",
            excluded_group_ids=excluded_group_ids,
            now=reconciled_at,
        )
        return ReconcileResult(
            item_id=snapshot.item_id,
            outcome=queued.outcome,
            job_id=queued.job.id,
            input_revision=queued.item.latest_input_revision,
        )

    item = _locked_processing_item(
        session,
        board_id=managed_board_id,
        item_id=snapshot.item_id,
    )
    if item is None:
        item = ProcessingItem(
            board_id=managed_board_id,
            item_id=snapshot.item_id,
            state=ProcessingItemState.INELIGIBLE.value,
        )
        session.add(item)
        session.flush([item])

    active_jobs = _active_jobs(session, item)
    running_job: ProcessingJob | None = None
    for job in active_jobs:
        if job.status == ProcessingJobStatus.RUNNING.value:
            running_job = running_job or job
            continue
        job.status = ProcessingJobStatus.CANCELLED.value
        job.completed_at = reconciled_at
        job.last_error = "InputIneligible" if board_managed else "BoardMoved"
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        _add_audit(
            session,
            item,
            job,
            event_type="operator_reconcile",
            outcome=(
                "cancelled_ineligible"
                if board_managed
                else "cancelled_moved_from_managed_board"
            ),
            details={"authoritativeBoardId": snapshot.board_id},
        )

    item.latest_input_revision = None
    item.latest_pipeline_version = None
    item.state = ProcessingItemState.INELIGIBLE.value
    item.supersession_requested_at = reconciled_at if running_job else None
    _add_audit(
        session,
        item,
        running_job,
        event_type="operator_reconcile",
        outcome=("ineligible" if board_managed else "moved_from_managed_board"),
        details={
            "active": snapshot.active,
            "authoritativeBoardId": snapshot.board_id,
            "boardManaged": board_managed,
            "groupExcluded": group_excluded,
            "groupId": snapshot.group_id,
        },
    )
    session.flush()
    return ReconcileResult(
        item_id=snapshot.item_id,
        outcome="ineligible",
        job_id=running_job.id if running_job else None,
        input_revision=None,
    )


def retry_failed_job(
    session: Session,
    job_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ProcessingJob:
    scheduled_at = now or utc_now()
    query = session.query(ProcessingJob).filter(ProcessingJob.id == job_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    failed_job = query.one_or_none()
    if failed_job is None:
        raise ValueError("processing job does not exist")
    if failed_job.status != ProcessingJobStatus.FAILED.value:
        raise ValueError("only a failed processing job can be retried")

    item = _locked_processing_item(
        session,
        board_id=failed_job.board_id,
        item_id=failed_job.item_id,
    )
    if item is None:
        raise RuntimeError("processing item does not exist")
    if (
        item.latest_input_revision != failed_job.input_revision
        or item.latest_pipeline_version != failed_job.pipeline_version
    ):
        raise ValueError("failed job input is no longer current; reconcile instead")
    if _active_jobs(session, item):
        raise ValueError("Sales item already has an active processing job")

    retry = ProcessingJob(
        board_id=failed_job.board_id,
        item_id=failed_job.item_id,
        trigger_type="operator_retry",
        input_revision=failed_job.input_revision,
        input_manifest_json=copy.deepcopy(failed_job.input_manifest_json),
        pipeline_version=failed_job.pipeline_version,
        status=ProcessingJobStatus.SCHEDULED.value,
        stage=failed_job.stage,
        scheduled_for=scheduled_at,
        max_attempts=failed_job.max_attempts,
        result_json=copy.deepcopy(failed_job.result_json),
    )
    session.add(retry)
    session.flush([retry])
    item.state = ProcessingItemState.SCHEDULED.value
    item.supersession_requested_at = None
    _add_audit(
        session,
        item,
        retry,
        event_type="operator_retry",
        outcome="scheduled",
        details={"failedJobId": str(failed_job.id)},
    )
    session.flush()
    return retry


def collect_processing_metrics(
    session: Session,
    *,
    lease_timeout_seconds: float,
    now: datetime | None = None,
) -> ProcessingMetrics:
    if lease_timeout_seconds <= 0:
        raise ValueError("lease_timeout_seconds must be positive")
    measured_at = now or utc_now()
    jobs_by_status: dict[str, int] = {}
    for job in session.query(ProcessingJob.status).all():
        jobs_by_status[job.status] = jobs_by_status.get(job.status, 0) + 1

    jobs_by_stage: dict[str, int] = {}
    for job in session.query(ProcessingJob.stage).all():
        stage = job.stage or "none"
        jobs_by_stage[stage] = jobs_by_stage.get(stage, 0) + 1

    items_by_state: dict[str, int] = {}
    for item in session.query(ProcessingItem.state).all():
        items_by_state[item.state] = items_by_state.get(item.state, 0) + 1

    runnable_times = [
        _aware(timestamp)
        for status, scheduled_for, next_retry_at in session.query(
            ProcessingJob.status,
            ProcessingJob.scheduled_for,
            ProcessingJob.next_retry_at,
        ).filter(
            or_(
                and_(
                    ProcessingJob.status == ProcessingJobStatus.SCHEDULED.value,
                    ProcessingJob.scheduled_for <= measured_at,
                ),
                and_(
                    ProcessingJob.status == ProcessingJobStatus.RETRY_WAIT.value,
                    ProcessingJob.next_retry_at.is_not(None),
                    ProcessingJob.next_retry_at <= measured_at,
                ),
            )
        )
        for timestamp in [
            next_retry_at
            if status == ProcessingJobStatus.RETRY_WAIT.value
            else scheduled_for
        ]
        if timestamp is not None
    ]
    oldest_age = (
        max(0.0, (measured_at - min(runnable_times)).total_seconds())
        if runnable_times
        else None
    )
    stale_before = measured_at - timedelta(seconds=lease_timeout_seconds)
    stale_running = (
        session.query(ProcessingJob)
        .filter(
            ProcessingJob.status == ProcessingJobStatus.RUNNING.value,
            or_(
                ProcessingJob.heartbeat_at < stale_before,
                and_(
                    ProcessingJob.heartbeat_at.is_(None),
                    ProcessingJob.locked_at < stale_before,
                ),
            ),
        )
        .count()
    )
    return ProcessingMetrics(
        jobs_by_status=jobs_by_status,
        jobs_by_stage=jobs_by_stage,
        items_by_state=items_by_state,
        oldest_runnable_age_seconds=oldest_age,
        stale_running_jobs=stale_running,
    )


def _authoritative_snapshot(
    client: IntakeItemReader,
    item_id: str,
    *,
    excluded_group_ids: Sequence[str] = (),
) -> SalesItemSnapshot:
    snapshot = parse_sales_item_snapshot(
        client.load_sales_item_intake(str(item_id)),
        contract=BOARD_CONTRACT,
        excluded_group_ids=excluded_group_ids,
    )
    if snapshot.item_id != str(item_id):
        raise ValueError("Monday returned the wrong Sales item")
    return snapshot


def _locked_processing_item(
    session: Session,
    *,
    board_id: str,
    item_id: str,
) -> ProcessingItem | None:
    query = session.query(ProcessingItem).filter_by(
        board_id=board_id,
        item_id=item_id,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.one_or_none()


def _active_jobs(
    session: Session,
    item: ProcessingItem,
) -> list[ProcessingJob]:
    query = session.query(ProcessingJob).filter(
        ProcessingJob.board_id == item.board_id,
        ProcessingJob.item_id == item.item_id,
        ProcessingJob.status.in_(ACTIVE_JOB_STATUSES),
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.all()


def _add_audit(
    session: Session,
    item: ProcessingItem,
    job: ProcessingJob | None,
    *,
    event_type: str,
    outcome: str,
    details: dict[str, object],
) -> None:
    session.add(
        ProcessingAudit(
            board_id=item.board_id,
            item_id=item.item_id,
            job_id=job.id if job else None,
            event_type=event_type,
            stage=job.stage if job else None,
            outcome=outcome,
            input_revision=job.input_revision if job else None,
            pipeline_version=job.pipeline_version if job else None,
            details_json=details,
        )
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
