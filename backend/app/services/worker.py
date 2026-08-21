"""Durable processing-job claims, leases, retries, and recovery."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import (
    ProcessingAudit,
    ProcessingItem,
    ProcessingItemState,
    ProcessingJob,
    ProcessingJobStatus,
)


class JobLeaseError(RuntimeError):
    """Raised when a worker no longer owns a running job."""


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    retried: int
    failed: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def claim_next_job(
    session: Session,
    *,
    worker_id: str,
    item_ids: Sequence[str] | None = None,
    now: datetime | None = None,
) -> ProcessingJob | None:
    """Claim one due job, skipping rows concurrently claimed by other workers."""

    normalized_worker_id = _worker_id(worker_id)
    claimed_at = now or utc_now()
    normalized_item_ids = (
        tuple(dict.fromkeys(str(value) for value in item_ids))
        if item_ids is not None
        else None
    )
    if normalized_item_ids == ():
        return None
    while True:
        query = (
            session.query(ProcessingJob)
            .join(
                ProcessingItem,
                and_(
                    ProcessingItem.board_id == ProcessingJob.board_id,
                    ProcessingItem.item_id == ProcessingJob.item_id,
                ),
            )
            .filter(
                or_(
                    and_(
                        ProcessingJob.status
                        == ProcessingJobStatus.SCHEDULED.value,
                        ProcessingJob.scheduled_for <= claimed_at,
                    ),
                    and_(
                        ProcessingJob.status
                        == ProcessingJobStatus.RETRY_WAIT.value,
                        ProcessingJob.next_retry_at.is_not(None),
                        ProcessingJob.next_retry_at <= claimed_at,
                    ),
                )
            )
            .order_by(
                ProcessingJob.scheduled_for.asc(),
                ProcessingJob.created_at.asc(),
                ProcessingJob.id.asc(),
            )
        )
        if normalized_item_ids is not None:
            query = query.filter(ProcessingJob.item_id.in_(normalized_item_ids))
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        job = query.first()
        if job is None:
            return None

        item = _locked_item(session, job)
        job.status = ProcessingJobStatus.RUNNING.value
        job.attempt_count += 1
        job.next_retry_at = None
        job.locked_at = claimed_at
        job.locked_by = normalized_worker_id
        job.heartbeat_at = claimed_at
        job.started_at = job.started_at or claimed_at
        job.last_error = None
        item.state = ProcessingItemState.PROCESSING.value
        _add_audit(
            session,
            item=item,
            job=job,
            event_type="worker_claim",
            outcome="claimed",
            details={
                "workerId": normalized_worker_id,
                "attemptCount": job.attempt_count,
            },
        )
        session.flush()
        return job


def heartbeat_job(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> None:
    job = lock_owned_job(session, job_id, worker_id=worker_id)
    job.heartbeat_at = now or utc_now()
    session.flush([job])


def complete_job(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    result: dict[str, object] | None = None,
    now: datetime | None = None,
) -> ProcessingJob:
    completed_at = now or utc_now()
    job = lock_owned_job(session, job_id, worker_id=worker_id)
    item = _locked_item(session, job)
    job.status = ProcessingJobStatus.COMPLETED.value
    job.completed_at = completed_at
    job.result_json = result if result is not None else job.result_json
    _release_lease(job)
    if not _job_is_superseded(item, job):
        item.state = ProcessingItemState.COMPLETED.value
        item.supersession_requested_at = None
    _add_audit(
        session,
        item=item,
        job=job,
        event_type="worker_completion",
        outcome="completed",
        details={"attemptCount": job.attempt_count},
    )
    session.flush()
    return job


def retry_or_fail_job(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    error: BaseException,
    retryable: bool = True,
    now: datetime | None = None,
    retry_base_seconds: float = 30.0,
    retry_max_seconds: float = 900.0,
) -> ProcessingJob:
    failed_at = now or utc_now()
    job = lock_owned_job(session, job_id, worker_id=worker_id)
    item = _locked_item(session, job)
    job.last_error = type(error).__name__
    should_retry = retryable and job.attempt_count < job.max_attempts
    if should_retry:
        delay = retry_delay_seconds(
            job.attempt_count,
            base_seconds=retry_base_seconds,
            max_seconds=retry_max_seconds,
        )
        job.status = ProcessingJobStatus.RETRY_WAIT.value
        job.next_retry_at = failed_at + timedelta(seconds=delay)
        item.state = ProcessingItemState.SCHEDULED.value
        outcome = "retry_scheduled"
        details: dict[str, object] = {
            "attemptCount": job.attempt_count,
            "errorType": type(error).__name__,
            "retryDelaySeconds": delay,
        }
    else:
        job.status = ProcessingJobStatus.FAILED.value
        job.completed_at = failed_at
        if not _job_is_superseded(item, job):
            item.state = ProcessingItemState.FAILED.value
        outcome = "failed"
        details = {
            "attemptCount": job.attempt_count,
            "errorType": type(error).__name__,
        }
    _release_lease(job)
    _add_audit(
        session,
        item=item,
        job=job,
        event_type="worker_failure",
        outcome=outcome,
        details=details,
    )
    session.flush()
    return job


def recover_stale_jobs(
    session: Session,
    *,
    lease_timeout_seconds: float,
    now: datetime | None = None,
    retry_base_seconds: float = 30.0,
    retry_max_seconds: float = 900.0,
) -> RecoveryResult:
    if lease_timeout_seconds <= 0:
        raise ValueError("lease_timeout_seconds must be positive")
    recovered_at = now or utc_now()
    stale_before = recovered_at - timedelta(seconds=lease_timeout_seconds)
    query = session.query(ProcessingJob).filter(
        ProcessingJob.status == ProcessingJobStatus.RUNNING.value,
        or_(
            ProcessingJob.heartbeat_at < stale_before,
            and_(
                ProcessingJob.heartbeat_at.is_(None),
                ProcessingJob.locked_at < stale_before,
            ),
        ),
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)

    retried = 0
    failed = 0
    for job in query.all():
        item = _locked_item(session, job)
        prior_worker_id = job.locked_by
        job.last_error = "WorkerLeaseExpired"
        if job.attempt_count < job.max_attempts:
            delay = retry_delay_seconds(
                job.attempt_count,
                base_seconds=retry_base_seconds,
                max_seconds=retry_max_seconds,
            )
            job.status = ProcessingJobStatus.RETRY_WAIT.value
            job.next_retry_at = recovered_at + timedelta(seconds=delay)
            item.state = ProcessingItemState.SCHEDULED.value
            outcome = "lease_recovered"
            retried += 1
        else:
            delay = None
            job.status = ProcessingJobStatus.FAILED.value
            job.completed_at = recovered_at
            if not _job_is_superseded(item, job):
                item.state = ProcessingItemState.FAILED.value
            outcome = "lease_expired"
            failed += 1
        _release_lease(job)
        _add_audit(
            session,
            item=item,
            job=job,
            event_type="worker_recovery",
            outcome=outcome,
            details={
                "priorWorkerId": prior_worker_id,
                "attemptCount": job.attempt_count,
                "retryDelaySeconds": delay,
            },
        )
    session.flush()
    return RecoveryResult(retried=retried, failed=failed)


def retry_delay_seconds(
    attempt_count: int,
    *,
    base_seconds: float = 30.0,
    max_seconds: float = 900.0,
) -> float:
    if attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    if base_seconds <= 0 or max_seconds <= 0:
        raise ValueError("retry delays must be positive")
    return min(base_seconds * (2 ** (attempt_count - 1)), max_seconds)


def lock_owned_job(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
) -> ProcessingJob:
    normalized_worker_id = _worker_id(worker_id)
    query = session.query(ProcessingJob).filter(ProcessingJob.id == job_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    job = query.one_or_none()
    if (
        job is None
        or job.status != ProcessingJobStatus.RUNNING.value
        or job.locked_by != normalized_worker_id
    ):
        raise JobLeaseError("processing job lease is not owned by this worker")
    return job


def _locked_item(session: Session, job: ProcessingJob) -> ProcessingItem:
    query = session.query(ProcessingItem).filter_by(
        board_id=job.board_id,
        item_id=job.item_id,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.one()


def _job_is_superseded(item: ProcessingItem, job: ProcessingJob) -> bool:
    return (
        item.latest_input_revision != job.input_revision
        or item.latest_pipeline_version != job.pipeline_version
    )


def _release_lease(job: ProcessingJob) -> None:
    job.locked_at = None
    job.locked_by = None
    job.heartbeat_at = None


def _add_audit(
    session: Session,
    *,
    item: ProcessingItem,
    job: ProcessingJob,
    event_type: str,
    outcome: str,
    details: dict[str, object],
) -> None:
    session.add(
        ProcessingAudit(
            board_id=item.board_id,
            item_id=item.item_id,
            job_id=job.id,
            event_type=event_type,
            stage=job.stage,
            outcome=outcome,
            input_revision=job.input_revision,
            pipeline_version=job.pipeline_version,
            details_json=details,
        )
    )


def _worker_id(value: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 255:
        raise ValueError("worker_id must contain 1 to 255 characters")
    return normalized