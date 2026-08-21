import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from app.config import BOARD_CONTRACT, DEFAULT_EXCLUDED_SALES_GROUP_IDS
from app.database import Base, create_database_engine, create_session_factory
from app.input_revision import EmailAssetIdentity, build_input_manifest
from app.models import (
    ProcessingItem,
    ProcessingItemState,
    ProcessingAudit,
    ProcessingJob,
    ProcessingJobStage,
    ProcessingJobStatus,
)
from app.publication_gate import PublicationGate
from app.services.accounts import AccountsIndexService
from app.services.pipeline import (
    PipelineDependencies,
    analysis_allowed,
    publication_allowed,
    run_pipeline_job,
)
from app.services.postcode import DesignParameterExtraction
from app.services.worker import claim_next_job, retry_or_fail_job


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def eml_bytes(
    body: str = "Please quote for the project at WA4 6NL.",
) -> bytes:
    message = EmailMessage()
    message["From"] = "Estimator <requester@acme.co.uk>"
    message["To"] = "sales@taperedplus.co.uk"
    message["Subject"] = "Project quote"
    message["Date"] = "Fri, 21 Aug 2026 11:00:00 +0100"
    message.set_content(body)
    return message.as_bytes()


def identity(content: bytes, asset_id: str = "10") -> EmailAssetIdentity:
    return EmailAssetIdentity(
        asset_id=asset_id,
        filename="request.eml",
        size_bytes=len(content),
        created_at=datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
    )


def sales_item(
    asset: EmailAssetIdentity,
    *,
    group_id: str = "topics",
) -> dict[str, Any]:
    return {
        "id": "42",
        "state": "active",
        "board": {"id": str(BOARD_CONTRACT.sales_board_id)},
        "group": {"id": group_id, "title": "Test Group"},
        "assets": [
            {
                "id": asset.asset_id,
                "name": asset.filename,
                "file_size": asset.size_bytes,
                "created_at": asset.created_at.isoformat(),
                "url": "https://files.monday.com/request.eml",
                "public_url": None,
            }
        ],
        "column_values": [
            {
                "id": BOARD_CONTRACT.email_file_column_id,
                "type": "file",
                "value": json.dumps({"files": [{"assetId": asset.asset_id}]}),
            }
        ],
    }


class FakeMonday:
    def __init__(self, item: Mapping[str, Any], content: bytes) -> None:
        self.item = item
        self.content = content
        self.download_count = 0
        self.mutations: list[Mapping[str, object]] = []

    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]:
        assert item_id == "42"
        return self.item

    def load_postcode_dropdown_column(self, board_id: int) -> Mapping[str, Any]:
        assert board_id == BOARD_CONTRACT.sales_board_id
        return {
            "id": BOARD_CONTRACT.postcode_column_id,
            "type": "dropdown",
            "settings": {"labels": [{"id": 115, "name": "WA"}]},
        }

    def download_asset(
        self,
        url: str,
        destination: Path,
        *,
        expected_size: int,
        expected_sha256: str | None = None,
    ) -> str:
        del url, expected_sha256
        assert expected_size == len(self.content)
        self.download_count += 1
        destination.write_bytes(self.content)
        return hashlib.sha256(self.content).hexdigest()

    def load_sales_item_for_publication(
        self, item_id: str
    ) -> Mapping[str, Any]:
        raise AssertionError(f"shadow mode attempted publication read for {item_id}")

    def change_sales_item_column_values(
        self,
        board_id: int,
        item_id: str,
        column_values: Mapping[str, object],
    ) -> None:
        del board_id, item_id
        self.mutations.append(column_values)


class FakePostcodeClient:
    def __init__(self) -> None:
        self.extraction_count = 0

    def process_pdf(self, content: bytes, filename: str) -> str:
        del content, filename
        return ""

    def process_image(
        self,
        content: bytes,
        filename: str,
        image_type: str = "ATTACHMENT",
    ) -> str:
        del content, filename, image_type
        return ""

    def extract_design_parameters(self, context: str) -> DesignParameterExtraction:
        assert "WA4 6NL" in context
        self.extraction_count += 1
        return DesignParameterExtraction(post_code="WA4 6NL", company="Acme")


class FlakyAccountsClient:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.page_calls = 0

    def load_accounts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        del cursor, limit
        assert board_id == BOARD_CONTRACT.accounts_board_id
        self.page_calls += 1
        if self.failures:
            self.failures -= 1
            raise TimeoutError("temporary Accounts read failure")
        return {
            "cursor": None,
            "items": [
                {
                    "id": "99",
                    "name": "Acme Limited",
                    "state": "active",
                    "board": {"id": str(BOARD_CONTRACT.accounts_board_id)},
                    "column_values": [
                        {
                            "id": BOARD_CONTRACT.account_email_domain_column_id,
                            "type": "text",
                            "text": "acme.co.uk",
                        },
                        {
                            "id": BOARD_CONTRACT.account_duplicate_column_id,
                            "type": "dropdown",
                            "values": [],
                        },
                    ],
                }
            ],
        }

    def load_account_item(self, item_id: str) -> Mapping[str, Any] | None:
        del item_id
        return None


@pytest.fixture
def database():
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    try:
        yield session_factory
    finally:
        engine.dispose()


def add_claimed_job(session_factory, asset: EmailAssetIdentity) -> ProcessingJob:
    from app.input_revision import compute_input_revision

    revision = compute_input_revision([asset])
    with session_factory() as session:
        item = ProcessingItem(
            board_id=str(BOARD_CONTRACT.sales_board_id),
            item_id="42",
            latest_input_revision=revision,
            latest_pipeline_version="test-v1",
            state=ProcessingItemState.SCHEDULED.value,
        )
        job = ProcessingJob(
            board_id=item.board_id,
            item_id=item.item_id,
            trigger_type="test",
            input_revision=revision,
            input_manifest_json=build_input_manifest([asset]),
            pipeline_version="test-v1",
            status=ProcessingJobStatus.SCHEDULED.value,
            stage=ProcessingJobStage.EXTRACTING.value,
            scheduled_for=NOW,
        )
        session.add_all([item, job])
        session.commit()
        claimed = claim_next_job(session, worker_id="worker-a", now=NOW)
        assert claimed is not None
        session.commit()
        return claimed


def dependencies(
    monday: FakeMonday,
    postcode_client: FakePostcodeClient,
    accounts_client: FlakyAccountsClient,
    *,
    excluded_group_ids: tuple[str, ...] = (),
) -> PipelineDependencies:
    return PipelineDependencies(
        monday=monday,
        postcode_client=postcode_client,
        accounts=AccountsIndexService(
            client=accounts_client,
            board_id=BOARD_CONTRACT.accounts_board_id,
            cache_ttl_seconds=0,
        ),
        publication_gate=PublicationGate(),
        internal_email_domains=("taperedplus.co.uk",),
        excluded_group_ids=excluded_group_ids,
    )


def test_shadow_pipeline_completes_without_monday_mutation(database) -> None:
    content = eml_bytes()
    asset = identity(content)
    job = add_claimed_job(database, asset)
    monday = FakeMonday(sales_item(asset), content)
    postcode_client = FakePostcodeClient()

    outcome = run_pipeline_job(
        database,
        job.id,
        worker_id="worker-a",
        dependencies=dependencies(
            monday,
            postcode_client,
            FlakyAccountsClient(),
        ),
        mode="shadow",
        now=NOW,
    )

    with database() as session:
        completed = session.get(ProcessingJob, job.id)
        item = session.query(ProcessingItem).one()
        assert outcome == "shadow_completed"
        assert completed is not None
        assert completed.status == ProcessingJobStatus.COMPLETED.value
        assert completed.result_json["publication"] == {
            "outcome": "shadow_skipped"
        }
        assert item.postcode_result_json["labelId"] == 115
        assert item.account_match_json["accountItemId"] == "99"
        assert monday.mutations == []


def test_pipeline_checkpoints_do_not_persist_raw_email_content(database) -> None:
    sensitive_marker = "PRIVATE-ENQUIRY-CONTENT-7F2A"
    content = eml_bytes(
        f"Please quote for the project at WA4 6NL. {sensitive_marker}"
    )
    asset = identity(content)
    job = add_claimed_job(database, asset)
    monday = FakeMonday(sales_item(asset), content)

    outcome = run_pipeline_job(
        database,
        job.id,
        worker_id="worker-a",
        dependencies=dependencies(
            monday,
            FakePostcodeClient(),
            FlakyAccountsClient(),
        ),
        mode="shadow",
        now=NOW,
    )

    with database() as session:
        completed = session.get(ProcessingJob, job.id)
        item = session.query(ProcessingItem).one()
        audits = session.query(ProcessingAudit).filter_by(job_id=job.id).all()
        assert completed is not None
        persisted = json.dumps(
            {
                "job": completed.result_json,
                "postcode": item.postcode_result_json,
                "account": item.account_match_json,
                "audits": [audit.details_json for audit in audits],
            },
            sort_keys=True,
        )

    assert outcome == "shadow_completed"
    assert sensitive_marker not in persisted


def test_retry_resumes_at_saved_matching_stage(database) -> None:
    content = eml_bytes()
    asset = identity(content)
    job = add_claimed_job(database, asset)
    monday = FakeMonday(sales_item(asset), content)
    postcode_client = FakePostcodeClient()
    accounts_client = FlakyAccountsClient(failures=1)
    pipeline_dependencies = dependencies(
        monday,
        postcode_client,
        accounts_client,
    )

    with pytest.raises(TimeoutError):
        run_pipeline_job(
            database,
            job.id,
            worker_id="worker-a",
            dependencies=pipeline_dependencies,
            mode="shadow",
            now=NOW,
        )

    with database() as session:
        saved = session.get(ProcessingJob, job.id)
        assert saved is not None
        assert saved.stage == ProcessingJobStage.MATCHING_ACCOUNT.value
        retry_or_fail_job(
            session,
            job.id,
            worker_id="worker-a",
            error=TimeoutError(),
            now=NOW,
        )
        session.commit()
        reclaimed = claim_next_job(
            session,
            worker_id="worker-b",
            now=NOW + timedelta(seconds=30),
        )
        assert reclaimed is not None
        session.commit()

    outcome = run_pipeline_job(
        database,
        job.id,
        worker_id="worker-b",
        dependencies=pipeline_dependencies,
        mode="shadow",
        now=NOW + timedelta(seconds=30),
    )

    assert outcome == "shadow_completed"
    assert monday.download_count == 1
    assert postcode_client.extraction_count == 1
    assert accounts_client.page_calls == 2


def test_changed_authoritative_input_cancels_and_queues_successor(database) -> None:
    original_content = eml_bytes()
    original_asset = identity(original_content)
    job = add_claimed_job(database, original_asset)
    changed_content = original_content + b"\n"
    changed_asset = identity(changed_content, asset_id="11")
    monday = FakeMonday(sales_item(changed_asset), changed_content)

    outcome = run_pipeline_job(
        database,
        job.id,
        worker_id="worker-a",
        dependencies=dependencies(
            monday,
            FakePostcodeClient(),
            FlakyAccountsClient(),
        ),
        mode="shadow",
        now=NOW,
    )

    with database() as session:
        jobs = session.query(ProcessingJob).order_by(ProcessingJob.created_at).all()
        item = session.query(ProcessingItem).one()
        assert outcome == "superseded"
        assert len(jobs) == 2
        assert jobs[0].status == ProcessingJobStatus.CANCELLED.value
        assert jobs[1].status == ProcessingJobStatus.SCHEDULED.value
        assert jobs[1].input_manifest_json[0]["asset_id"] == "11"
        assert jobs[1].trigger_type == "input_supersession"
        assert item.latest_input_revision == jobs[1].input_revision
        assert monday.download_count == 0


def test_excluded_group_cancels_queued_job_before_processing(database) -> None:
    content = eml_bytes()
    asset = identity(content)
    job = add_claimed_job(database, asset)
    excluded_group_id = DEFAULT_EXCLUDED_SALES_GROUP_IDS[0]
    monday = FakeMonday(
        sales_item(asset, group_id=excluded_group_id),
        content,
    )
    accounts_client = FlakyAccountsClient()

    outcome = run_pipeline_job(
        database,
        job.id,
        worker_id="worker-a",
        dependencies=dependencies(
            monday,
            FakePostcodeClient(),
            accounts_client,
            excluded_group_ids=(excluded_group_id,),
        ),
        mode="shadow",
        now=NOW,
    )

    with database() as session:
        cancelled = session.get(ProcessingJob, job.id)
        audit = (
            session.query(ProcessingAudit)
            .filter_by(job_id=job.id, event_type="group_exclusion")
            .one()
        )
        assert outcome == "superseded"
        assert cancelled is not None
        assert cancelled.status == ProcessingJobStatus.CANCELLED.value
        assert cancelled.last_error == "GroupExcluded"
        assert cancelled.item.state == ProcessingItemState.INELIGIBLE.value
        assert audit.outcome == "excluded_group"
        assert audit.details_json["groupId"] == excluded_group_id
        assert monday.download_count == 0
        assert accounts_client.page_calls == 0


@pytest.mark.parametrize(
    ("mode", "item_id", "analysis", "publication"),
    [
        ("off", "42", False, False),
        ("shadow", "42", True, False),
        ("allowlist", "42", True, True),
        ("allowlist", "99", False, False),
        ("enabled", "42", True, True),
    ],
)
def test_processing_mode_policy(
    mode: str,
    item_id: str,
    analysis: bool,
    publication: bool,
) -> None:
    allowlist = ("42",)

    assert analysis_allowed(mode, item_id, allowlist) is analysis  # type: ignore[arg-type]
    assert publication_allowed(mode, item_id, allowlist) is publication  # type: ignore[arg-type]
