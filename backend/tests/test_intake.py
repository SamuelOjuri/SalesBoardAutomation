import json
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.config import BOARD_CONTRACT, DEFAULT_EXCLUDED_SALES_GROUP_IDS
from app.database import Base, create_database_engine, create_session_factory
from app.input_revision import compute_input_revision
from app.models import (
    ProcessingAudit,
    ProcessingItem,
    ProcessingJob,
    ProcessingJobStatus,
    WebhookEvent,
)
from app.services.intake import (
    IntakeContractError,
    download_email_assets,
    parse_sales_item_snapshot,
    queue_sales_item_snapshot,
)


def item_snapshot() -> dict[str, Any]:
    return {
        "id": "42",
        "state": "active",
        "board": {"id": str(BOARD_CONTRACT.sales_board_id)},
        "group": {"id": "topics", "title": "Outstanding Emails"},
        "assets": [
            {
                "id": "10",
                "name": "notes.pdf",
                "file_size": 20,
                "created_at": "2026-08-19T09:30:00Z",
                "url": "https://auth.monday.com/notes.pdf",
                "public_url": "https://files.monday.com/public/notes.pdf?token=one",
            },
            {
                "id": "2",
                "name": "request.EML",
                "file_size": "12",
                "created_at": "2026-08-19T09:31:00Z",
                "url": "https://auth.monday.com/request.eml",
                "public_url": (
                    "https://files.monday.com/public/request.eml?token=two"
                ),
            },
        ],
        "column_values": [
            {
                "id": BOARD_CONTRACT.email_file_column_id,
                "type": "file",
                "value": json.dumps(
                    {"files": [{"assetId": 10}, {"assetId": 2}]}
                ),
            }
        ],
    }


def test_snapshot_uses_only_supported_email_file_members() -> None:
    snapshot = parse_sales_item_snapshot(item_snapshot(), contract=BOARD_CONTRACT)

    assert snapshot.active is True
    assert snapshot.board_id == str(BOARD_CONTRACT.sales_board_id)
    assert snapshot.group_id == "topics"
    assert [asset.identity.asset_id for asset in snapshot.email_assets] == ["2"]
    assert snapshot.email_assets[0].identity.size_bytes == 12
    assert snapshot.email_assets[0].download_url == (
        "https://files.monday.com/public/request.eml?token=two"
    )


def test_snapshot_does_not_use_assets_outside_email_file_membership() -> None:
    raw_item = item_snapshot()
    raw_item["column_values"][0]["value"] = json.dumps({"files": []})

    snapshot = parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)

    assert snapshot.email_assets == ()


def test_excluded_group_snapshot_does_not_require_email_file_column() -> None:
    excluded_group_id = DEFAULT_EXCLUDED_SALES_GROUP_IDS[0]
    raw_item = item_snapshot()
    raw_item["group"]["id"] = excluded_group_id
    raw_item["column_values"] = []

    snapshot = parse_sales_item_snapshot(
        raw_item,
        contract=BOARD_CONTRACT,
        excluded_group_ids=(excluded_group_id,),
    )

    assert snapshot.group_id == excluded_group_id
    assert snapshot.email_assets == ()


def test_moved_board_snapshot_does_not_require_email_file_column() -> None:
    raw_item = item_snapshot()
    raw_item["board"]["id"] = "1882196103"
    raw_item["group"] = {"id": "group_mkpbd6vy", "title": "Landing Zone"}
    raw_item["column_values"] = []

    snapshot = parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)

    assert snapshot.board_id == "1882196103"
    assert snapshot.group_id == "group_mkpbd6vy"
    assert snapshot.email_assets == ()


def test_eligible_group_snapshot_still_requires_email_file_column() -> None:
    raw_item = item_snapshot()
    raw_item["column_values"] = []

    with pytest.raises(IntakeContractError) as captured:
        parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)

    assert captured.value.code == "email_file_column_missing"
    assert captured.value.retryable is True


def test_snapshot_requires_an_authoritative_group() -> None:
    raw_item = item_snapshot()
    raw_item.pop("group")

    with pytest.raises(IntakeContractError, match="item group"):
        parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("file_size", None, "asset size"),
        ("created_at", "2026-08-19T09:31:00", "timezone"),
        ("public_url", None, "public download URL is missing"),
        ("public_url", "http://files.monday.com/request.eml", "HTTPS"),
    ],
)
def test_supported_asset_requires_complete_safe_metadata(
    field: str, value: object, message: str
) -> None:
    raw_item = item_snapshot()
    raw_item["assets"][1][field] = value

    with pytest.raises(IntakeContractError, match=message):
        parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)


def test_identity_only_snapshot_does_not_require_download_url() -> None:
    raw_item = item_snapshot()
    raw_item["assets"][1]["public_url"] = None

    snapshot = parse_sales_item_snapshot(
        raw_item,
        contract=BOARD_CONTRACT,
        require_download_urls=False,
    )

    assert snapshot.email_assets[0].identity.asset_id == "2"
    assert snapshot.email_assets[0].download_url is None


def test_missing_asset_metadata_has_safe_retryable_reason_code() -> None:
    raw_item = item_snapshot()
    raw_item["assets"] = []

    with pytest.raises(IntakeContractError) as captured:
        parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)

    assert captured.value.code == "asset_metadata_missing"
    assert captured.value.retryable is True


def test_malformed_membership_has_safe_terminal_reason_code() -> None:
    raw_item = item_snapshot()
    raw_item["column_values"][0]["value"] = "not-json"

    with pytest.raises(IntakeContractError) as captured:
        parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)

    assert captured.value.code == "email_file_value_malformed"
    assert captured.value.retryable is False


def test_queue_coalesces_without_mutating_active_job_identity() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    snapshot = parse_sales_item_snapshot(item_snapshot(), contract=BOARD_CONTRACT)

    try:
        with session_factory() as session:
            first_event = WebhookEvent(
                idempotency_key="trigger:first",
                payload_json={},
                authenticated=True,
            )
            second_event = WebhookEvent(
                idempotency_key="trigger:second",
                payload_json={},
                authenticated=True,
            )
            session.add_all([first_event, second_event])
            session.flush()
            first = queue_sales_item_snapshot(
                session,
                snapshot,
                webhook_event_id=first_event.id,
                pipeline_version="test-v1",
                now=datetime(2026, 8, 19, tzinfo=timezone.utc),
            )
            original_revision = first.job.input_revision

            changed_raw_item = item_snapshot()
            changed_raw_item["assets"][1]["file_size"] = 13
            changed = parse_sales_item_snapshot(
                changed_raw_item, contract=BOARD_CONTRACT
            )
            second = queue_sales_item_snapshot(
                session,
                changed,
                webhook_event_id=second_event.id,
                pipeline_version="test-v1",
                now=datetime(2026, 8, 20, tzinfo=timezone.utc),
            )
            session.commit()

            assert first.outcome == "queued"
            assert second.outcome == "coalesced"
            assert session.query(ProcessingJob).count() == 1
            assert session.query(ProcessingAudit).count() == 2
            assert second.job.input_revision == original_revision
            assert second.item.latest_input_revision != original_revision
            assert second.item.supersession_requested_at is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("inserted", "active_job_exists"),
    [(True, False), (False, False), (False, True)],
)
def test_postgres_intake_insert_locks_item_before_queueing(
    inserted: bool,
    active_job_exists: bool,
) -> None:
    snapshot = parse_sales_item_snapshot(item_snapshot(), contract=BOARD_CONTRACT)
    item = ProcessingItem(
        id=uuid.uuid4(), board_id=snapshot.board_id, item_id=snapshot.item_id
    )
    job = ProcessingJob(
        id=uuid.uuid4(),
        input_revision=compute_input_revision(
            tuple(asset.identity for asset in snapshot.email_assets)
        ),
        pipeline_version="test-v1",
        status=ProcessingJobStatus.RUNNING.value,
    )
    session = MagicMock(spec=Session)
    session.bind = MagicMock()
    session.bind.dialect = postgresql.dialect()
    item_query = session.query.return_value.filter_by.return_value
    locked_item_query = item_query.with_for_update.return_value
    locked_item_query.one_or_none.return_value = None
    locked_item_query.one.return_value = item
    job_query = session.query.return_value.filter.return_value.order_by.return_value
    job_query.with_for_update.return_value.first.return_value = (
        job if active_job_exists else None
    )
    session.execute.return_value.rowcount = int(inserted)

    result = queue_sales_item_snapshot(session, snapshot, pipeline_version="test-v1")

    session.execute.assert_called_once()
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "INSERT INTO processing_items" in str(compiled)
    assert (
        "ON CONFLICT ON CONSTRAINT uq_processing_items_board_item DO NOTHING"
        in str(compiled)
    )
    assert compiled.params["board_id"] == snapshot.board_id
    assert compiled.params["item_id"] == snapshot.item_id
    assert item_query.with_for_update.call_count == 2
    locked_item_query.one.assert_called_once()
    assert result.item is item
    if active_job_exists:
        session.begin_nested.assert_not_called()
        assert result.job is job
        assert result.outcome == "coalesced"
        assert result.created_job is False
    else:
        session.begin_nested.assert_called_once()
        assert result.job.input_revision == job.input_revision
        assert result.job.status == ProcessingJobStatus.SCHEDULED.value
        assert result.outcome == "queued"
        assert result.created_job is True


def test_download_context_uses_asset_order_and_always_cleans_up() -> None:
    snapshot = parse_sales_item_snapshot(item_snapshot(), contract=BOARD_CONTRACT)
    downloaded_paths: list[Path] = []

    class Downloader:
        def download_asset(
            self,
            url: str,
            destination: Path,
            *,
            expected_size: int,
            expected_sha256: str | None = None,
        ) -> str:
            content = b"x" * expected_size
            destination.write_bytes(content)
            downloaded_paths.append(destination)
            return hashlib.sha256(content).hexdigest()

    with pytest.raises(RuntimeError, match="stop processing"):
        with download_email_assets(Downloader(), snapshot.email_assets) as assets:
            assert [asset.identity.asset_id for asset in assets] == ["2"]
            assert assets[0].path.exists()
            raise RuntimeError("stop processing")

    assert downloaded_paths
    assert all(not path.exists() for path in downloaded_paths)
