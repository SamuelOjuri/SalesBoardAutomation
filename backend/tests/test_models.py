from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import Base, create_database_engine, create_session_factory
from app.models import ProcessingItem, ProcessingJob, ProcessingJobStatus


def create_job(item: ProcessingItem, revision: str) -> ProcessingJob:
    return ProcessingJob(
        board_id=item.board_id,
        item_id=item.item_id,
        trigger_type="webhook",
        input_revision=revision,
        input_manifest_json=[
            {
                "asset_id": "1",
                "filename": "request.eml",
                "size_bytes": 10,
                "created_at": "2026-08-19T09:30:00.000000Z",
            }
        ],
        pipeline_version="phase-1-test",
        status=ProcessingJobStatus.SCHEDULED.value,
        scheduled_for=datetime.now(timezone.utc),
    )


@pytest.fixture
def database() -> tuple[object, object]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    try:
        yield engine, session_factory
    finally:
        engine.dispose()


def test_only_one_active_job_is_allowed_per_sales_item(database: tuple[object, object]) -> None:
    _, session_factory = database
    with session_factory() as session:
        item = ProcessingItem(board_id="5100711564", item_id="123")
        session.add(item)
        session.flush()
        session.add(create_job(item, "a" * 64))
        session.commit()

        session.add(create_job(item, "b" * 64))
        with pytest.raises(IntegrityError):
            session.commit()


def test_completed_job_allows_a_successor(database: tuple[object, object]) -> None:
    _, session_factory = database
    with session_factory() as session:
        item = ProcessingItem(board_id="5100711564", item_id="456")
        first_job = create_job(item, "a" * 64)
        session.add_all([item, first_job])
        session.commit()

        first_job.status = ProcessingJobStatus.COMPLETED.value
        session.add(create_job(item, "b" * 64))
        session.commit()


def test_job_input_identity_cannot_change(database: tuple[object, object]) -> None:
    _, session_factory = database
    with session_factory() as session:
        item = ProcessingItem(board_id="5100711564", item_id="789")
        job = create_job(item, "a" * 64)
        session.add_all([item, job])
        session.commit()

        job.input_revision = "b" * 64
        with pytest.raises(ValueError, match="immutable"):
            session.commit()