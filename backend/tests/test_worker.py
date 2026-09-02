import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from app.config import Settings, build_processing_pipeline_version
from app.database import Base, create_database_engine, create_session_factory
from app.models import (
    ProcessingAudit,
    ProcessingItem,
    ProcessingItemState,
    ProcessingJob,
    ProcessingJobStage,
    ProcessingJobStatus,
)
from app.services.email_parser import AttachmentExtractionError
from app.services.intake import IntakeContractError, IntakeSnapshotUnavailable
from app.services.worker import (
    JobLeaseError,
    claim_next_job,
    heartbeat_job,
    recover_stale_jobs,
    retry_or_fail_job,
)
from app.worker import WorkerRuntime, process_next_job


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session_factory():
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def add_job(
    session,
    *,
    item_id: str,
    scheduled_for: datetime = NOW,
    revision: str = "a" * 64,
    latest_revision: str | None = None,
    status: str = ProcessingJobStatus.SCHEDULED.value,
    attempt_count: int = 0,
    max_attempts: int = 3,
) -> ProcessingJob:
    item = ProcessingItem(
        board_id="5100711564",
        item_id=item_id,
        latest_input_revision=latest_revision or revision,
        latest_pipeline_version="test-v1",
        state=ProcessingItemState.SCHEDULED.value,
    )
    job = ProcessingJob(
        board_id=item.board_id,
        item_id=item.item_id,
        trigger_type="test",
        input_revision=revision,
        input_manifest_json=[
            {
                "asset_id": item_id,
                "filename": "request.eml",
                "size_bytes": 10,
                "created_at": "2026-08-21T11:00:00.000000Z",
            }
        ],
        pipeline_version="test-v1",
        status=status,
        stage=ProcessingJobStage.EXTRACTING.value,
        scheduled_for=scheduled_for,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )
    session.add_all([item, job])
    session.flush()
    return job


def test_claims_oldest_due_job_and_records_lease(session_factory) -> None:
    with session_factory() as session:
        expected = add_job(
            session,
            item_id="101",
            scheduled_for=NOW - timedelta(minutes=1),
        )
        add_job(
            session,
            item_id="102",
            scheduled_for=NOW + timedelta(minutes=1),
        )

        claimed = claim_next_job(session, worker_id="worker-a", now=NOW)
        session.commit()

        assert claimed is not None
        assert claimed.id == expected.id
        assert claimed.status == ProcessingJobStatus.RUNNING.value
        assert claimed.attempt_count == 1
        assert claimed.locked_by == "worker-a"
        assert claimed.locked_at == NOW
        assert claimed.heartbeat_at == NOW
        assert claimed.item.state == ProcessingItemState.PROCESSING.value
        audit = session.query(ProcessingAudit).one()
        assert audit.event_type == "worker_claim"
        assert audit.details_json["workerId"] == "worker-a"


def test_heartbeat_requires_current_lease_owner(session_factory) -> None:
    with session_factory() as session:
        job = add_job(session, item_id="201")
        claim_next_job(session, worker_id="worker-a", now=NOW)

        with pytest.raises(JobLeaseError):
            heartbeat_job(
                session,
                job.id,
                worker_id="worker-b",
                now=NOW + timedelta(seconds=10),
            )

        heartbeat_job(
            session,
            job.id,
            worker_id="worker-a",
            now=NOW + timedelta(seconds=10),
        )
        assert job.heartbeat_at == NOW + timedelta(seconds=10)


def test_failure_preserves_stage_and_schedules_exponential_retry(
    session_factory,
) -> None:
    with session_factory() as session:
        job = add_job(session, item_id="301", attempt_count=1)
        claimed = claim_next_job(session, worker_id="worker-a", now=NOW)
        assert claimed is not None
        claimed.stage = ProcessingJobStage.MATCHING_ACCOUNT.value
        claimed.result_json = {"completedStage": "extracting"}

        retry_or_fail_job(
            session,
            job.id,
            worker_id="worker-a",
            error=TimeoutError("sensitive upstream message"),
            now=NOW,
        )
        session.commit()

        assert job.status == ProcessingJobStatus.RETRY_WAIT.value
        assert job.next_retry_at == NOW + timedelta(seconds=60)
        assert job.stage == ProcessingJobStage.MATCHING_ACCOUNT.value
        assert job.result_json == {"completedStage": "extracting"}
        assert job.last_error == "TimeoutError"
        assert job.locked_by is None
        assert "sensitive" not in str(job.last_error)


def test_worker_logs_only_error_type_not_sensitive_message(
    session_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_marker = "PRIVATE-EMAIL-CONTENT-92C1"
    with session_factory() as session:
        job = add_job(session, item_id="351")
        job_id = job.id
        session.commit()

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://user:password@localhost/sales",
        monday_ingestion_access_token="token",
        monday_webhook_shared_secret="shared-secret",
        gemini_api_key="gemini-key",
        gemini_model="gemini-test-model",
        processing_pipeline_version=build_processing_pipeline_version(
            "gemini-test-model"
        ),
        processing_mode="shadow",
        worker_heartbeat_interval_seconds=1,
        worker_lease_timeout_seconds=10,
    )
    runtime = WorkerRuntime(
        settings=settings,
        engine=cast(Any, session_factory.kw["bind"]),
        session_factory=session_factory,
        monday_client=cast(Any, None),
        dependencies=cast(Any, None),
        worker_id="worker-sensitive-log-test",
    )

    def fail_pipeline(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(sensitive_marker)

    caplog.set_level(logging.INFO, logger="app.worker")

    assert process_next_job(
        runtime,
        pipeline_runner=fail_pipeline,
        now=NOW,
    )

    with session_factory() as session:
        failed = session.get(ProcessingJob, job_id)
        assert failed is not None
        assert failed.last_error == "RuntimeError"
    failure_record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("processing job failed ")
    )
    assert failure_record.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert failure_record.stage == ProcessingJobStage.EXTRACTING.value  # type: ignore[attr-defined]
    assert failure_record.attempt_count == 1  # type: ignore[attr-defined]
    assert failure_record.outcome == ProcessingJobStatus.RETRY_WAIT.value  # type: ignore[attr-defined]
    assert sensitive_marker not in caplog.text
    assert sensitive_marker not in str(failure_record.__dict__)


def test_retryable_intake_snapshot_failure_records_safe_reason(
    session_factory,
) -> None:
    with session_factory() as session:
        job = add_job(session, item_id="353")
        job_id = job.id
        session.commit()

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://user:password@localhost/sales",
        monday_ingestion_access_token="token",
        monday_webhook_shared_secret="shared-secret",
        gemini_api_key="gemini-key",
        gemini_model="gemini-test-model",
        processing_pipeline_version=build_processing_pipeline_version(
            "gemini-test-model"
        ),
        processing_mode="shadow",
        worker_heartbeat_interval_seconds=1,
        worker_lease_timeout_seconds=10,
    )
    runtime = WorkerRuntime(
        settings=settings,
        engine=cast(Any, session_factory.kw["bind"]),
        session_factory=session_factory,
        monday_client=cast(Any, None),
        dependencies=cast(Any, None),
        worker_id="worker-intake-retry-test",
    )

    def fail_pipeline(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise IntakeSnapshotUnavailable(
            "PRIVATE-SNAPSHOT-CONTENT",
            code="asset_metadata_missing",
        )

    assert process_next_job(runtime, pipeline_runner=fail_pipeline, now=NOW)

    with session_factory() as session:
        retried = session.get(ProcessingJob, job_id)
        assert retried is not None
        assert retried.status == ProcessingJobStatus.RETRY_WAIT.value
        failure_audit = (
            session.query(ProcessingAudit)
            .filter_by(job_id=job_id, event_type="worker_failure")
            .one()
        )
        assert failure_audit.details_json["errorCode"] == "asset_metadata_missing"
        assert failure_audit.details_json["rootCauseType"] == (
            "IntakeSnapshotUnavailable"
        )


def test_malformed_intake_failure_remains_terminal(session_factory) -> None:
    with session_factory() as session:
        job = add_job(session, item_id="354")
        job_id = job.id
        session.commit()

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://user:password@localhost/sales",
        monday_ingestion_access_token="token",
        monday_webhook_shared_secret="shared-secret",
        gemini_api_key="gemini-key",
        gemini_model="gemini-test-model",
        processing_pipeline_version=build_processing_pipeline_version(
            "gemini-test-model"
        ),
        processing_mode="shadow",
        worker_heartbeat_interval_seconds=1,
        worker_lease_timeout_seconds=10,
    )
    runtime = WorkerRuntime(
        settings=settings,
        engine=cast(Any, session_factory.kw["bind"]),
        session_factory=session_factory,
        monday_client=cast(Any, None),
        dependencies=cast(Any, None),
        worker_id="worker-intake-terminal-test",
    )

    def fail_pipeline(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise IntakeContractError(
            "PRIVATE-SNAPSHOT-CONTENT",
            code="email_file_value_malformed",
        )

    assert process_next_job(runtime, pipeline_runner=fail_pipeline, now=NOW)

    with session_factory() as session:
        failed = session.get(ProcessingJob, job_id)
        assert failed is not None
        assert failed.status == ProcessingJobStatus.FAILED.value
        assert failed.attempt_count == 1


def test_supported_attachment_extraction_failure_is_retried(
    session_factory,
) -> None:
    with session_factory() as session:
        job = add_job(session, item_id="352")
        job_id = job.id
        session.commit()

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://user:password@localhost/sales",
        monday_ingestion_access_token="token",
        monday_webhook_shared_secret="shared-secret",
        gemini_api_key="gemini-key",
        gemini_model="gemini-test-model",
        processing_pipeline_version=build_processing_pipeline_version(
            "gemini-test-model"
        ),
        processing_mode="shadow",
        worker_heartbeat_interval_seconds=1,
        worker_lease_timeout_seconds=10,
    )
    runtime = WorkerRuntime(
        settings=settings,
        engine=cast(Any, session_factory.kw["bind"]),
        session_factory=session_factory,
        monday_client=cast(Any, None),
        dependencies=cast(Any, None),
        worker_id="worker-attachment-retry-test",
    )

    def fail_pipeline(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AttachmentExtractionError(
            "supported .pdf attachment text extraction failed"
        )

    assert process_next_job(
        runtime,
        pipeline_runner=fail_pipeline,
        now=NOW,
    )

    with session_factory() as session:
        retried = session.get(ProcessingJob, job_id)
        assert retried is not None
        assert retried.status == ProcessingJobStatus.RETRY_WAIT.value
        assert retried.attempt_count == 1
        assert retried.last_error == "AttachmentExtractionError"
        assert retried.next_retry_at is not None
        failure_audit = (
            session.query(ProcessingAudit)
            .filter_by(job_id=job_id, event_type="worker_failure")
            .one()
        )
        assert failure_audit.outcome == "retry_scheduled"
        assert failure_audit.details_json["retryDelaySeconds"] == 30


def test_stale_worker_recovery_retries_or_exhausts_job(session_factory) -> None:
    with session_factory() as session:
        retry_job = add_job(
            session,
            item_id="401",
            status=ProcessingJobStatus.RUNNING.value,
            attempt_count=1,
        )
        failed_job = add_job(
            session,
            item_id="402",
            status=ProcessingJobStatus.RUNNING.value,
            attempt_count=3,
            max_attempts=3,
        )
        for job in (retry_job, failed_job):
            job.locked_by = "dead-worker"
            job.locked_at = NOW - timedelta(minutes=10)
            job.heartbeat_at = NOW - timedelta(minutes=10)

        result = recover_stale_jobs(
            session,
            lease_timeout_seconds=300,
            now=NOW,
        )
        session.commit()

        assert result.retried == 1
        assert result.failed == 1
        assert retry_job.status == ProcessingJobStatus.RETRY_WAIT.value
        assert retry_job.next_retry_at == NOW + timedelta(seconds=30)
        assert failed_job.status == ProcessingJobStatus.FAILED.value
        assert failed_job.item.state == ProcessingItemState.FAILED.value
        assert retry_job.locked_by is None
        assert failed_job.locked_by is None


def test_claim_leases_superseded_job_for_authoritative_reconciliation(
    session_factory,
) -> None:
    with session_factory() as session:
        superseded = add_job(
            session,
            item_id="501",
            revision="a" * 64,
            latest_revision="b" * 64,
            scheduled_for=NOW - timedelta(minutes=2),
        )
        add_job(
            session,
            item_id="502",
            scheduled_for=NOW - timedelta(minutes=1),
        )

        claimed = claim_next_job(session, worker_id="worker-a", now=NOW)
        session.commit()

        assert claimed is not None
        assert claimed.id == superseded.id
        assert superseded.status == ProcessingJobStatus.RUNNING.value
        assert superseded.locked_by == "worker-a"
        assert (
            session.query(ProcessingAudit)
            .filter_by(event_type="input_supersession")
            .count()
            == 0
        )


def test_recovery_ignores_live_heartbeat(session_factory) -> None:
    with session_factory() as session:
        job = add_job(
            session,
            item_id="601",
            status=ProcessingJobStatus.RUNNING.value,
            attempt_count=1,
        )
        job.locked_by = "live-worker"
        job.locked_at = NOW - timedelta(minutes=10)
        job.heartbeat_at = NOW - timedelta(seconds=10)

        result = recover_stale_jobs(
            session,
            lease_timeout_seconds=300,
            now=NOW,
        )

        assert result.retried == 0
        assert result.failed == 0
        assert job.status == ProcessingJobStatus.RUNNING.value
        assert job.locked_by == "live-worker"


def test_claim_can_be_restricted_to_allowlisted_items(session_factory) -> None:
    with session_factory() as session:
        add_job(session, item_id="701", scheduled_for=NOW - timedelta(minutes=2))
        allowed = add_job(
            session,
            item_id="702",
            scheduled_for=NOW - timedelta(minutes=1),
        )

        claimed = claim_next_job(
            session,
            worker_id="worker-a",
            item_ids=("702",),
            now=NOW,
        )

        assert claimed is not None
        assert claimed.id == allowed.id
