import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import BOARD_CONTRACT
from app.database import Base, create_database_engine, create_session_factory
from app.models import ProcessingAudit, ProcessingJob, WebhookEvent
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
                "url": "https://files.monday.com/notes.pdf",
            },
            {
                "id": "2",
                "name": "request.EML",
                "file_size": "12",
                "created_at": "2026-08-19T09:31:00Z",
                "url": "https://files.monday.com/request.eml",
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


def test_snapshot_does_not_use_assets_outside_email_file_membership() -> None:
    raw_item = item_snapshot()
    raw_item["column_values"][0]["value"] = json.dumps({"files": []})

    snapshot = parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)

    assert snapshot.email_assets == ()


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
        ("url", "http://files.monday.com/request.eml", "HTTPS"),
    ],
)
def test_supported_asset_requires_complete_safe_metadata(
    field: str, value: object, message: str
) -> None:
    raw_item = item_snapshot()
    raw_item["assets"][1][field] = value

    with pytest.raises(IntakeContractError, match=message):
        parse_sales_item_snapshot(raw_item, contract=BOARD_CONTRACT)


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
