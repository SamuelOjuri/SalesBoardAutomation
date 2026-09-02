import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.config import BOARD_CONTRACT, DEFAULT_EXCLUDED_SALES_GROUP_IDS
from app.database import Base, create_database_engine, create_session_factory
from app.models import (
    ProcessingAudit,
    ProcessingItem,
    ProcessingItemState,
    ProcessingJob,
    ProcessingJobStage,
    ProcessingJobStatus,
)
from app.services.operations import (
    collect_processing_metrics,
    reconcile_sales_item,
    retry_failed_job,
)


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
    status: str,
    scheduled_for: datetime = NOW,
) -> ProcessingJob:
    revision = item_id.zfill(64)
    item = ProcessingItem(
        board_id=str(BOARD_CONTRACT.sales_board_id),
        item_id=item_id,
        latest_input_revision=revision,
        latest_pipeline_version="test-v1",
        state=ProcessingItemState.FAILED.value,
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
                "created_at": "2026-08-21T10:00:00.000000Z",
            }
        ],
        pipeline_version="test-v1",
        status=status,
        stage=ProcessingJobStage.MATCHING_ACCOUNT.value,
        scheduled_for=scheduled_for,
        attempt_count=3,
        max_attempts=3,
        result_json={
            "schemaVersion": 1,
            "completedStage": "extracting",
        },
    )
    session.add_all([item, job])
    session.flush()
    return job


class InactiveMonday:
    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]:
        return {
            "id": item_id,
            "state": "archived",
            "board": {"id": str(BOARD_CONTRACT.sales_board_id)},
            "group": {"id": "topics", "title": "Outstanding Emails"},
            "assets": [],
            "column_values": [
                {
                    "id": BOARD_CONTRACT.email_file_column_id,
                    "type": "file",
                    "value": json.dumps({"files": []}),
                }
            ],
        }


class ExcludedMonday:
    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]:
        return {
            "id": item_id,
            "state": "active",
            "board": {"id": str(BOARD_CONTRACT.sales_board_id)},
            "group": {
                "id": DEFAULT_EXCLUDED_SALES_GROUP_IDS[0],
                "title": "Completed Folder",
            },
            "assets": [],
            "column_values": [],
        }


class MovedMonday:
    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]:
        return {
            "id": item_id,
            "state": "active",
            "board": {"id": "1882196103"},
            "group": {"id": "group_mkpbd6vy", "title": "Landing Zone"},
            "assets": [],
            "column_values": [],
        }


def test_retry_failed_job_clones_saved_stage_as_new_identity_owner(
    session_factory,
) -> None:
    with session_factory() as session:
        failed = add_job(
            session,
            item_id="71",
            status=ProcessingJobStatus.FAILED.value,
        )
        session.commit()

        retry = retry_failed_job(session, failed.id, now=NOW)
        session.commit()

        assert retry.id != failed.id
        assert retry.status == ProcessingJobStatus.SCHEDULED.value
        assert retry.stage == ProcessingJobStage.MATCHING_ACCOUNT.value
        assert retry.result_json == failed.result_json
        assert retry.result_json is not failed.result_json
        assert retry.input_revision == failed.input_revision
        assert retry.input_manifest_json == failed.input_manifest_json
        assert retry.attempt_count == 0
        assert retry.trigger_type == "operator_retry"
        assert retry.item.state == ProcessingItemState.SCHEDULED.value


def test_retry_rejects_a_failed_job_whose_input_is_no_longer_current(
    session_factory,
) -> None:
    with session_factory() as session:
        failed = add_job(
            session,
            item_id="72",
            status=ProcessingJobStatus.FAILED.value,
        )
        session.commit()
        failed.item.latest_input_revision = "f" * 64
        session.commit()

        with pytest.raises(ValueError, match="reconcile"):
            retry_failed_job(session, failed.id, now=NOW)


def test_reconcile_ineligible_item_cancels_scheduled_job(session_factory) -> None:
    with session_factory() as session:
        job = add_job(
            session,
            item_id="73",
            status=ProcessingJobStatus.SCHEDULED.value,
        )
        session.commit()

        result = reconcile_sales_item(
            session,
            InactiveMonday(),
            "73",
            pipeline_version="test-v1",
            now=NOW,
        )
        session.commit()

        assert result.outcome == "ineligible"
        assert job.status == ProcessingJobStatus.CANCELLED.value
        assert job.item.state == ProcessingItemState.INELIGIBLE.value
        assert job.item.latest_input_revision is None
        assert job.item.latest_pipeline_version is None


def test_reconcile_excluded_item_without_email_column_marks_ineligible(
    session_factory,
) -> None:
    excluded_group_id = DEFAULT_EXCLUDED_SALES_GROUP_IDS[0]
    with session_factory() as session:
        job = add_job(
            session,
            item_id="76",
            status=ProcessingJobStatus.FAILED.value,
        )
        session.commit()

        result = reconcile_sales_item(
            session,
            ExcludedMonday(),
            "76",
            pipeline_version="test-v1",
            excluded_group_ids=(excluded_group_id,),
            now=NOW,
        )
        session.commit()

        assert result.outcome == "ineligible"
        assert result.job_id is None
        assert job.status == ProcessingJobStatus.FAILED.value
        assert job.item.state == ProcessingItemState.INELIGIBLE.value
        assert job.item.latest_input_revision is None
        assert job.item.latest_pipeline_version is None


def test_reconcile_moved_item_uses_original_board_identity(session_factory) -> None:
    with session_factory() as session:
        job = add_job(
            session,
            item_id="77",
            status=ProcessingJobStatus.FAILED.value,
        )
        session.commit()

        result = reconcile_sales_item(
            session,
            MovedMonday(),
            "77",
            pipeline_version="test-v1",
            now=NOW,
        )
        session.commit()

        audit = (
            session.query(ProcessingAudit)
            .filter_by(
                board_id=str(BOARD_CONTRACT.sales_board_id),
                item_id="77",
                event_type="operator_reconcile",
                outcome="moved_from_managed_board",
            )
            .one()
        )
        items = session.query(ProcessingItem).all()
        assert result.outcome == "ineligible"
        assert result.job_id is None
        assert job.status == ProcessingJobStatus.FAILED.value
        assert job.item.state == ProcessingItemState.INELIGIBLE.value
        assert job.item.latest_input_revision is None
        assert job.item.latest_pipeline_version is None
        assert len(items) == 1
        assert items[0].board_id == str(BOARD_CONTRACT.sales_board_id)
        assert audit.details_json == {
            "active": True,
            "authoritativeBoardId": "1882196103",
            "boardManaged": False,
            "groupExcluded": False,
            "groupId": "group_mkpbd6vy",
        }


def test_metrics_report_queue_age_stages_and_stale_leases(session_factory) -> None:
    with session_factory() as session:
        scheduled = add_job(
            session,
            item_id="74",
            status=ProcessingJobStatus.SCHEDULED.value,
            scheduled_for=NOW - timedelta(minutes=2),
        )
        running = add_job(
            session,
            item_id="75",
            status=ProcessingJobStatus.RUNNING.value,
        )
        running.locked_by = "dead-worker"
        running.locked_at = NOW - timedelta(minutes=10)
        running.heartbeat_at = NOW - timedelta(minutes=10)
        session.commit()

        metrics = collect_processing_metrics(
            session,
            lease_timeout_seconds=300,
            now=NOW,
        )

        assert metrics.jobs_by_status == {"scheduled": 1, "running": 1}
        assert metrics.jobs_by_stage == {"matching_account": 2}
        assert metrics.oldest_runnable_age_seconds == 120
        assert metrics.stale_running_jobs == 1
        assert scheduled.item.item_id == "74"
