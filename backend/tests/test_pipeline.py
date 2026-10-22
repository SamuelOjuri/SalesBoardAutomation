import copy
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
import requests
from sqlalchemy.orm.attributes import flag_modified

from app.config import BOARD_CONTRACT, DEFAULT_EXCLUDED_SALES_GROUP_IDS
from app.database import Base, create_database_engine, create_session_factory
from app.input_revision import EmailAssetIdentity, build_input_manifest
from app.monday_client import MondayClient
from app.models import (
    ProcessingItem,
    ProcessingItemState,
    ProcessingAudit,
    ProcessingJob,
    ProcessingJobStage,
    ProcessingJobStatus,
)
from app.publication_gate import PublicationGate
from app.services.accounts import AccountsIndexService, ContactsIndexService
from app.services.pipeline import (
    PipelineDependencies,
    analysis_allowed,
    publication_allowed,
    run_pipeline_job,
)
from app.services.postcode import DesignParameterExtraction
from app.services.postcode_evidence import PostcodeEvidence
from app.services.worker import claim_next_job, retry_or_fail_job
from app.services.intake import parse_sales_item_snapshot, queue_sales_item_snapshot


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def eml_bytes(
    body: str = "Please quote for the project at WA4 6NL.",
    *,
    from_value: str = "Estimator <requester@acme.co.uk>",
) -> bytes:
    message = EmailMessage()
    message["From"] = from_value
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
                "public_url": "https://files.monday.com/public/request.eml",
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
        self.intake_read_count = 0
        self.schema_read_count = 0
        self.mutations: list[Mapping[str, object]] = []

    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]:
        assert item_id == "42"
        self.intake_read_count += 1
        return self.item

    def load_postcode_dropdown_column(self, board_id: int) -> Mapping[str, Any]:
        assert board_id == BOARD_CONTRACT.sales_board_id
        self.schema_read_count += 1
        return {
            "id": BOARD_CONTRACT.postcode_column_id,
            "type": "dropdown",
            "settings": {
                "labels": [{"id": 115, "label": "WA", "is_deactivated": False}]
            },
        }

    def load_sales_item_for_extraction(
        self, item_id: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        return (
            self.load_sales_item_intake(item_id),
            self.load_postcode_dropdown_column(BOARD_CONTRACT.sales_board_id),
        )

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
    def __init__(
        self,
        *,
        requester_domain: str = "acme.co.uk",
        company: str = "Acme",
    ) -> None:
        self.extraction_count = 0
        self.requester_domain = requester_domain
        self.company = company

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

    def extract_design_parameters(
        self,
        context: str,
        *,
        requester_domain: str | None = None,
        requester_source: str = "not_found",
        internal_company_aliases: tuple[str, ...] | list[str] = (),
    ) -> DesignParameterExtraction:
        assert "WA4 6NL" in context
        assert requester_domain == self.requester_domain
        assert requester_source == "top_level_sender"
        del internal_company_aliases
        self.extraction_count += 1
        return DesignParameterExtraction(
            post_code="WA4 6NL",
            company=self.company,
            postcode_evidence=PostcodeEvidence(
                project_identity=None, current_project_quote=None,
                source_project_quote=None, address_quote="WA4 6NL",
            ),
        )


class FlakyAccountsClient:
    def __init__(
        self,
        failures: int = 0,
        *,
        account_id: str = "99",
        account_name: str = "Acme Limited",
        account_domain: str = "acme.co.uk",
    ) -> None:
        self.failures = failures
        self.account_id = account_id
        self.account_name = account_name
        self.account_domain = account_domain
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
                    "id": self.account_id,
                    "name": self.account_name,
                    "state": "active",
                    "board": {"id": str(BOARD_CONTRACT.accounts_board_id)},
                    "column_values": [
                        {
                            "id": BOARD_CONTRACT.account_email_domain_column_id,
                            "type": "text",
                            "text": self.account_domain,
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

    def load_contacts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        del cursor, limit
        assert board_id == BOARD_CONTRACT.contacts_board_id
        return {"cursor": None, "items": []}


class ContactAwareAccountsClient:
    def __init__(self) -> None:
        self.account_page_calls = 0
        self.contact_page_calls = 0

    def load_accounts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        del cursor, limit
        assert board_id == BOARD_CONTRACT.accounts_board_id
        self.account_page_calls += 1
        accounts = (
            ("1661824839", "Encon Insulation Limited"),
            ("1661825541", "ECON Leeds"),
            ("1661825559", "ECON Plymouth"),
        )
        return {
            "cursor": None,
            "items": [
                {
                    "id": item_id,
                    "name": name,
                    "state": "active",
                    "board": {"id": str(BOARD_CONTRACT.accounts_board_id)},
                    "column_values": [
                        {
                            "id": BOARD_CONTRACT.account_email_domain_column_id,
                            "type": "text",
                            "text": "encon.co.uk",
                        },
                        {
                            "id": BOARD_CONTRACT.account_duplicate_column_id,
                            "type": "dropdown",
                            "values": [],
                        },
                    ],
                }
                for item_id, name in accounts
            ],
        }

    def load_account_item(self, item_id: str) -> Mapping[str, Any] | None:
        del item_id
        return None

    def load_contacts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        del cursor, limit
        assert board_id == BOARD_CONTRACT.contacts_board_id
        self.contact_page_calls += 1
        email = "s.morrissey@encon.co.uk"
        return {
            "cursor": None,
            "items": [
                {
                    "id": "1662713491",
                    "name": "Sarah Morrissey",
                    "state": "active",
                    "board": {"id": str(BOARD_CONTRACT.contacts_board_id)},
                    "column_values": [
                        {
                            "id": BOARD_CONTRACT.contact_email_column_id,
                            "type": "email",
                            "email": email,
                            "text": email,
                        },
                        {
                            "id": (
                                BOARD_CONTRACT.contact_accounts_relation_column_id
                            ),
                            "type": "board_relation",
                            "linked_item_ids": ["1661824839"],
                        },
                    ],
                }
            ],
        }


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
    accounts_client: Any,
    *,
    excluded_group_ids: tuple[str, ...] = (),
    account_requester_domain_aliases: Mapping[
        str, tuple[str, ...]
    ] | None = None,
) -> PipelineDependencies:
    monday.accounts_client = accounts_client
    return PipelineDependencies(
        monday=monday,
        postcode_client=postcode_client,
        accounts=AccountsIndexService(
            client=accounts_client,
            board_id=BOARD_CONTRACT.accounts_board_id,
            cache_ttl_seconds=0,
        ),
        contacts=ContactsIndexService(
            client=accounts_client,
            board_id=BOARD_CONTRACT.contacts_board_id,
            cache_ttl_seconds=0,
        ),
        publication_gate=PublicationGate(),
        internal_email_domains=("taperedplus.co.uk",),
        excluded_group_ids=excluded_group_ids,
        account_requester_domain_aliases=(
            account_requester_domain_aliases or {}
        ),
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
        assert completed.result_json["requester"]["websiteDomains"] == []
        assert item.postcode_result_json["labelId"] == 115
        assert item.account_match_json["accountItemId"] == "99"
        assert monday.mutations == []


def test_pipeline_resolves_shared_domain_by_unique_contact_email(database) -> None:
    requester_email = "s.morrissey@encon.co.uk"
    content = eml_bytes(
        from_value=f"Sarah Morrissey <{requester_email}>",
    )
    asset = identity(content)
    job = add_claimed_job(database, asset)
    monday = FakeMonday(sales_item(asset), content)
    accounts_client = ContactAwareAccountsClient()

    outcome = run_pipeline_job(
        database,
        job.id,
        worker_id="worker-a",
        dependencies=dependencies(
            monday,
            FakePostcodeClient(
                requester_domain="encon.co.uk",
                company="Encon Group",
            ),
            accounts_client,
        ),
        mode="shadow",
        now=NOW,
    )

    with database() as session:
        completed = session.get(ProcessingJob, job.id)
        item = session.query(ProcessingItem).one()
        assert completed is not None
        assert outcome == "shadow_completed"
        assert completed.result_json["requester"]["emailAddressSha256"] == (
            hashlib.sha256(requester_email.encode("utf-8")).hexdigest()
        )
        assert requester_email not in json.dumps(completed.result_json).casefold()
        assert completed.result_json["account"]["reason"] == (
            "unique_contact_email"
        )
        assert completed.result_json["account"]["accountItemId"] == (
            "1661824839"
        )
        assert completed.result_json["account"]["contactItemId"] == (
            "1662713491"
        )
        assert item.account_match_json["contactCandidateIds"] == [
            "1662713491"
        ]
    assert accounts_client.account_page_calls == 1
    assert accounts_client.contact_page_calls == 1


def test_revalidation_does_not_require_download_url_after_extraction(
    database,
) -> None:
    class ExpiringDownloadUrlMonday(FakeMonday):
        def __init__(self, item: Mapping[str, Any], content: bytes) -> None:
            super().__init__(item, content)
            self.intake_reads = 0

        def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]:
            item = super().load_sales_item_intake(item_id)
            self.intake_reads += 1
            if self.intake_reads == 1:
                return item
            revalidation_item = dict(item)
            revalidation_item["assets"] = [
                {**asset, "public_url": None} for asset in item["assets"]
            ]
            return revalidation_item

    content = eml_bytes()
    asset = identity(content)
    job = add_claimed_job(database, asset)
    monday = ExpiringDownloadUrlMonday(sales_item(asset), content)

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
        assert completed is not None
        assert completed.status == ProcessingJobStatus.COMPLETED.value
    assert outcome == "shadow_completed"
    assert monday.intake_reads == 3
    assert monday.download_count == 1


def test_account_alias_uses_corroborating_wrapped_signature_website(
    database,
) -> None:
    content = eml_bytes(
        (
            "Please quote for the project at WA4 6NL.\n\n"
            "Best Regards\nJamie Dunsmore\n"
            "W: https://linkprotect.cudasvc.com/url?"
            "a=https%3A%2F%2Faccuroof.co.uk&c=opaque\n"
            "Registered company: SIG PLC"
        ),
        from_value='"Dunsmore, Jamie" <jamiedunsmore@sigplc.com>',
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
            FakePostcodeClient(
                requester_domain="sigplc.com",
                company="SIG PLC",
            ),
            FlakyAccountsClient(
                account_id="1661824807",
                account_name="AccuRoof",
                account_domain="accuroof.co.uk",
            ),
            account_requester_domain_aliases={
                "1661824807": ("sigplc.com",),
            },
        ),
        mode="shadow",
        now=NOW,
    )

    with database() as session:
        completed = session.get(ProcessingJob, job.id)
        assert completed is not None
        assert outcome == "shadow_completed"
        assert completed.result_json["requester"]["websiteDomains"] == [
            "accuroof.co.uk"
        ]
        assert completed.result_json["account"]["accountItemId"] == (
            "1661824807"
        )
        assert completed.result_json["account"]["reason"] == (
            "unique_domain_alias_website"
        )


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
    excluded_item = sales_item(asset, group_id=excluded_group_id)
    excluded_item["column_values"] = []
    monday = FakeMonday(
        excluded_item,
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


def test_moved_board_cancels_queued_job_before_processing(database) -> None:
    content = eml_bytes()
    asset = identity(content)
    job = add_claimed_job(database, asset)
    moved_item = sales_item(asset, group_id="group_mkpbd6vy")
    moved_item["board"]["id"] = "1882196103"
    moved_item["column_values"] = []
    monday = FakeMonday(moved_item, content)
    accounts_client = FlakyAccountsClient()

    outcome = run_pipeline_job(
        database,
        job.id,
        worker_id="worker-a",
        dependencies=dependencies(
            monday,
            FakePostcodeClient(),
            accounts_client,
        ),
        mode="shadow",
        now=NOW,
    )

    with database() as session:
        cancelled = session.get(ProcessingJob, job.id)
        audit = (
            session.query(ProcessingAudit)
            .filter_by(job_id=job.id, event_type="board_movement")
            .one()
        )
        assert outcome == "superseded"
        assert cancelled is not None
        assert cancelled.status == ProcessingJobStatus.CANCELLED.value
        assert cancelled.last_error == "BoardMoved"
        assert cancelled.item.state == ProcessingItemState.INELIGIBLE.value
        assert cancelled.item.board_id == str(BOARD_CONTRACT.sales_board_id)
        assert audit.outcome == "moved_from_managed_board"
        assert audit.details_json["authoritativeBoardId"] == "1882196103"
        assert audit.details_json["groupId"] == "group_mkpbd6vy"
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


class PublishingMonday(FakeMonday):
    def __init__(self, item: Mapping[str, Any], content: bytes) -> None:
        super().__init__(item, content)
        self.postcode_ids: list[str] = []
        self.account_ids: list[str] = []
        self.postcode_labels = [
            {"id": 115, "label": "WA", "is_deactivated": False}
        ]

    def load_postcode_dropdown_column(self, board_id: int) -> Mapping[str, Any]:
        column = dict(super().load_postcode_dropdown_column(board_id))
        column["settings"] = {"labels": self.postcode_labels}
        return column

    def load_sales_item_for_publication(self, item_id: str) -> Mapping[str, Any]:
        assert item_id == "42"
        item = dict(self.item)
        item["column_values"] = list(item["column_values"]) + [
            {
                "id": BOARD_CONTRACT.postcode_column_id,
                "type": "dropdown",
                "values": [{"id": value} for value in self.postcode_ids],
            },
            {
                "id": BOARD_CONTRACT.accounts_relation_column_id,
                "type": "board_relation",
                "linked_item_ids": self.account_ids,
            },
        ]
        return item

    def load_sales_item_and_account_for_publication(
        self, item_id: str, account_item_id: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        return (
            self.load_sales_item_for_publication(item_id),
            self.accounts_client.load_account_item(account_item_id),
        )

    def change_sales_item_column_values(
        self, board_id: int, item_id: str, column_values: Mapping[str, object]
    ) -> None:
        super().change_sales_item_column_values(board_id, item_id, column_values)
        if BOARD_CONTRACT.postcode_column_id in column_values:
            self.postcode_ids = [
                str(value)
                for value in column_values[BOARD_CONTRACT.postcode_column_id]["ids"]
            ]
        if BOARD_CONTRACT.accounts_relation_column_id in column_values:
            self.account_ids = [
                str(value)
                for value in column_values[BOARD_CONTRACT.accounts_relation_column_id]["item_ids"]
            ]


class EligibleAccountsClient(FlakyAccountsClient):
    def load_account_item(self, item_id: str) -> Mapping[str, Any] | None:
        if item_id != self.account_id:
            return None
        return FlakyAccountsClient(
            account_id=self.account_id,
            account_name=self.account_name,
            account_domain=self.account_domain,
        ).load_accounts_page(BOARD_CONTRACT.accounts_board_id)["items"][0]


def enable_publication(deps: PipelineDependencies) -> None:
    deps.publication_gate.apply_schema({
        "id": BOARD_CONTRACT.sales_board_id,
        "columns": [
            {"id": BOARD_CONTRACT.email_file_column_id, "type": "file"},
            {
                "id": BOARD_CONTRACT.accounts_relation_column_id,
                "type": "board_relation",
                "settings": {"board_ids": [BOARD_CONTRACT.accounts_board_id]},
            },
            {
                "id": BOARD_CONTRACT.postcode_column_id,
                "type": "dropdown",
                "settings": {"labels": [
                    {"id": label.id, "label": label.name}
                    for label in BOARD_CONTRACT.required_postcode_labels
                ]},
            },
        ],
    })
    assert deps.publication_gate.publication_enabled


class PipelineHttpSession:
    """Exercise the real client's HTTP boundary with mutable Monday state."""

    def __init__(self, live: PublishingMonday) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []
        self.live = live
        self.account = EligibleAccountsClient().load_account_item("99")
        self.mutation_error: BaseException | None = None
        self.post_read_error: BaseException | None = None
        self.before_publication = None

    def post(self, url: str, **kwargs: Any):
        del url
        request = kwargs["json"]
        operation = request["query"].split()[1].split("(")[0]
        self.calls.append(operation)
        if operation == "AccountsPage":
            data = {"boards": [{"id": str(BOARD_CONTRACT.accounts_board_id),
                                "items_page": {"cursor": None, "items": [self.account]}}]}
        elif operation == "SalesItemIntake":
            data = {"items": [self.live.load_sales_item_intake("42")]}
        elif operation == "SalesItemExtraction":
            item, column = self.live.load_sales_item_for_extraction("42")
            data = {"items": [item], "boards": [{
                "id": str(BOARD_CONTRACT.sales_board_id), "columns": [column],
            }]}
        elif operation == "SalesItemAndAccountPublication":
            if self.before_publication is not None:
                self.before_publication()
            data = {
                "sales_items": [self.live.load_sales_item_for_publication("42")],
                "account_items": [self.account] if self.account is not None else [],
            }
        elif operation == "ChangeSalesItemColumns":
            self.live.change_sales_item_column_values(
                BOARD_CONTRACT.sales_board_id, "42",
                json.loads(request["variables"]["column_values"]),
            )
            if self.mutation_error is not None:
                raise self.mutation_error
            data = {"change_multiple_column_values": {"id": "42"}}
        elif operation == "SalesItemPublication":
            if self.post_read_error is not None:
                raise self.post_read_error
            data = {"items": [self.live.load_sales_item_for_publication("42")]}
        else:
            raise AssertionError(f"unexpected GraphQL request: {operation}")

        class Response:
            status_code = 200

            def json(self):
                return {"data": copy.deepcopy(data)}

        return Response()


@pytest.fixture
def http_pipeline(database, monkeypatch):
    content = eml_bytes()
    live = PublishingMonday(sales_item(identity(content)), content)
    transport = PipelineHttpSession(live)
    monday = MondayClient(access_token="test", api_version="2026-07", session=transport)
    monkeypatch.setattr(monday, "download_asset", live.download_asset)
    model = FakePostcodeClient()
    deps = replace(
        dependencies(monday, model, monday),
        accounts=AccountsIndexService(
            client=monday, board_id=BOARD_CONTRACT.accounts_board_id,
            cache_ttl_seconds=300,
        ),
    )
    enable_publication(deps)
    # Exclude reference-index refreshes, as in the analysis's nine-call baseline.
    deps.accounts.load_index()
    transport.calls.clear()
    snapshot = parse_sales_item_snapshot(
        monday.load_sales_item_intake("42"), contract=BOARD_CONTRACT,
    )
    with database() as session:
        queued = queue_sales_item_snapshot(session, snapshot, pipeline_version="test-v1", now=NOW)
        session.commit()
        job = claim_next_job(session, worker_id="worker-a", now=NOW)
        assert job is not None and job.id == queued.job.id
        session.commit()
    return job, deps, transport, model


def test_successful_publication_uses_six_graphql_requests_including_intake(
    database, http_pipeline,
) -> None:
    job, deps, transport, model = http_pipeline
    assert run_pipeline_job(
        database, job.id, worker_id="worker-a", dependencies=deps, mode="enabled", now=NOW,
    ) == "completed"
    assert transport.calls == [
        "SalesItemIntake",  # Webhook intake.
        "SalesItemExtraction",  # Sales snapshot and live dropdown schema.
        "SalesItemIntake",  # Matching input check.
        "SalesItemAndAccountPublication",  # Final validation of both snapshots.
        "ChangeSalesItemColumns",
        "SalesItemPublication",  # Authoritative post-write confirmation.
    ]
    assert transport.live.postcode_ids == ["115"]
    assert transport.live.account_ids == ["99"]
    assert len(transport.live.mutations) == model.extraction_count == 1


@pytest.mark.parametrize("change", ["input", "group", "board", "inactive"])
def test_batched_final_validation_catches_changes_after_matching(
    database, http_pipeline, change,
) -> None:
    job, deps, transport, model = http_pipeline
    deps = replace(deps, excluded_group_ids=DEFAULT_EXCLUDED_SALES_GROUP_IDS)

    def change_live_sales():
        item = transport.live.item
        if change == "input":
            item["assets"][0]["name"] = "changed.eml"
        elif change == "group":
            item["group"]["id"] = DEFAULT_EXCLUDED_SALES_GROUP_IDS[0]
        elif change == "board":
            item["board"]["id"] = "999"
        else:
            item["state"] = "archived"

    transport.before_publication = change_live_sales
    assert run_pipeline_job(
        database, job.id, worker_id="worker-a", dependencies=deps, mode="enabled", now=NOW,
    ) == "superseded"
    assert model.extraction_count == 1
    assert transport.live.mutations == []
    with database() as session:
        assert session.get(ProcessingJob, job.id).status == ProcessingJobStatus.CANCELLED.value
        assert session.query(ProcessingJob).count() == (2 if change == "input" else 1)


@pytest.mark.parametrize("stage", ["validating", "publishing"])
@pytest.mark.parametrize("change", ["human_values", "duplicate_account", "input"])
def test_resumed_validation_and_publication_fetch_fresh_snapshots(
    database, http_pipeline, monkeypatch, stage, change,
) -> None:
    import app.services.pipeline as pipeline

    job, deps, transport, model = http_pipeline

    def interrupt_stage(*args, **kwargs):
        raise requests.Timeout("worker interrupted before final validation")

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, f"_run_{'validation' if stage == 'validating' else 'publication'}_stage", interrupt_stage)
        with pytest.raises(requests.Timeout):
            run_pipeline_job(
                database, job.id, worker_id="worker-a", dependencies=deps, mode="enabled", now=NOW,
            )
    with database() as session:
        saved = session.get(ProcessingJob, job.id)
        assert saved.stage == stage
        retry_or_fail_job(session, job.id, worker_id="worker-a", error=requests.Timeout(), now=NOW)
        session.commit()
        assert claim_next_job(session, worker_id="worker-b", now=NOW + timedelta(seconds=30)) is not None
        session.commit()

    if change == "human_values":
        transport.live.postcode_ids = ["114"]
        transport.live.account_ids = ["777"]
    elif change == "duplicate_account":
        transport.account["column_values"][1]["values"] = [{"id": 1}]
    else:
        transport.live.item["assets"][0]["name"] = "changed.eml"
    transport.calls.clear()
    outcome = run_pipeline_job(
        database, job.id, worker_id="worker-b", dependencies=deps, mode="enabled",
        now=NOW + timedelta(seconds=30),
    )

    assert transport.calls[0] == "SalesItemAndAccountPublication"
    assert model.extraction_count == 1
    if change == "human_values":
        assert outcome == "completed"
        assert transport.live.mutations == []
        assert transport.live.postcode_ids == ["114"]
        assert transport.live.account_ids == ["777"]
        assert len(transport.calls) == 1
    elif change == "duplicate_account":
        assert outcome == "completed"
        assert transport.live.mutations == [{BOARD_CONTRACT.postcode_column_id: {"ids": [115]}}]
        assert transport.calls[-1] == "SalesItemPublication"
    else:
        assert outcome == "superseded"
        assert transport.live.mutations == []


def test_ambiguous_batched_publication_still_confirms_both_fields(database, http_pipeline) -> None:
    job, deps, transport, model = http_pipeline
    transport.mutation_error = requests.Timeout("response lost after Monday accepted the write")
    assert run_pipeline_job(
        database, job.id, worker_id="worker-a", dependencies=deps, mode="enabled", now=NOW,
    ) == "completed"
    assert transport.calls[-2:] == ["ChangeSalesItemColumns", "SalesItemPublication"]
    assert len(transport.live.mutations) == 1
    with database() as session:
        publication = session.get(ProcessingJob, job.id).result_json["publication"]
        assert publication["mutationWasAmbiguous"] is True
        assert publication["postcode"]["outcome"] == publication["accounts"]["outcome"] == "published"


def test_failed_confirmation_resumes_with_fresh_batch_and_preserves_human_values(
    database, http_pipeline,
) -> None:
    job, deps, transport, model = http_pipeline
    transport.post_read_error = requests.Timeout("confirmation unavailable")
    with pytest.raises(requests.Timeout):
        run_pipeline_job(
            database, job.id, worker_id="worker-a", dependencies=deps, mode="enabled", now=NOW,
        )
    with database() as session:
        assert session.get(ProcessingJob, job.id).stage == ProcessingJobStage.PUBLISHING.value
        retry_or_fail_job(session, job.id, worker_id="worker-a", error=requests.Timeout(), now=NOW)
        session.commit()
        assert claim_next_job(session, worker_id="worker-b", now=NOW + timedelta(seconds=30)) is not None
        session.commit()
    transport.live.postcode_ids = ["114"]
    transport.live.account_ids = ["777"]
    transport.post_read_error = None
    transport.calls.clear()
    assert run_pipeline_job(
        database, job.id, worker_id="worker-b", dependencies=deps, mode="enabled",
        now=NOW + timedelta(seconds=30),
    ) == "completed"
    assert transport.calls == ["SalesItemAndAccountPublication"]
    assert len(transport.live.mutations) == model.extraction_count == 1
    assert transport.live.postcode_ids == ["114"]
    assert transport.live.account_ids == ["777"]


def run_queued_enquiry(
    database,
    monday: FakeMonday,
    deps: PipelineDependencies,
    *,
    mode: str = "shadow",
    pipeline_version: str = "test-v1",
    force_reprocess: bool = False,
) -> ProcessingJob:
    snapshot = parse_sales_item_snapshot(monday.item, contract=BOARD_CONTRACT)
    with database() as session:
        now = NOW + timedelta(seconds=session.query(ProcessingJob).count())
        queued = queue_sales_item_snapshot(
            session,
            snapshot,
            pipeline_version=pipeline_version,
            force_reprocess=force_reprocess,
            now=now,
        )
        session.commit()
        claimed = claim_next_job(session, worker_id="worker-a", now=now)
        assert claimed is not None and claimed.id == queued.job.id
        session.commit()
    run_pipeline_job(
        database, claimed.id, worker_id="worker-a", dependencies=deps,
        mode=mode, now=now,
    )
    with database() as session:
        return session.get(ProcessingJob, claimed.id)


@pytest.fixture
def completed_enquiry(database):
    content = eml_bytes()
    monday = PublishingMonday(sales_item(identity(content)), content)
    model = FakePostcodeClient()
    accounts = EligibleAccountsClient()
    deps = dependencies(monday, model, accounts)
    enable_publication(deps)
    completed = run_queued_enquiry(database, monday, deps, mode="enabled")
    return monday, model, accounts, deps, completed


def test_repeated_completed_enquiry_reuses_extraction_and_preserves_human_edits(
    database, completed_enquiry,
) -> None:
    monday, model, accounts, deps, first = completed_enquiry
    monday.postcode_ids = ["114"]
    monday.account_ids = ["777"]
    second = run_queued_enquiry(database, monday, deps, mode="enabled")

    assert second.id != first.id
    assert second.status == ProcessingJobStatus.COMPLETED.value
    assert second.result_json["reusedExtractionJobId"] == str(first.id)
    assert second.result_json["publication"]["postcode"]["outcome"] == "preserved_existing"
    assert second.result_json["publication"]["accounts"]["outcome"] == "preserved_existing"
    assert monday.postcode_ids == ["114"]
    assert monday.account_ids == ["777"]
    assert len(monday.mutations) == 1
    assert model.extraction_count == 1
    assert monday.download_count == 1
    assert monday.intake_read_count == 3  # Extraction is reused; validation is at publication.
    assert monday.schema_read_count == 2  # Both mappings use live settings.
    assert accounts.page_calls == 2
    with database() as session:
        audit = session.query(ProcessingAudit).filter_by(
            job_id=second.id, event_type="pipeline_stage", outcome="reused"
        ).one()
        assert audit.details_json["completedStage"] == ProcessingJobStage.EXTRACTING.value
        assert session.get(ProcessingJob, first.id).result_json == first.result_json


def test_unresolved_shadow_result_can_publish_with_changed_accounts_without_extraction(
    database,
) -> None:
    content = eml_bytes()
    monday = PublishingMonday(sales_item(identity(content)), content)
    model = FakePostcodeClient()
    accounts = EligibleAccountsClient(account_name="Other", account_domain="other.co.uk")
    deps = dependencies(monday, model, accounts)
    enable_publication(deps)
    first = run_queued_enquiry(database, monday, deps)
    assert first.result_json["account"]["accountItemId"] is None
    assert first.result_json["publication"] == {"outcome": "shadow_skipped"}

    accounts.account_id = "100"
    accounts.account_name = "Acme Limited"
    accounts.account_domain = "acme.co.uk"
    second = run_queued_enquiry(database, monday, deps, mode="enabled")

    assert second.result_json["reusedExtractionJobId"] == str(first.id)
    assert second.result_json["account"]["accountItemId"] == "100"
    assert second.result_json["publication"]["outcome"] == "published"
    assert monday.account_ids == ["100"]
    assert monday.postcode_ids == ["115"]
    assert model.extraction_count == monday.download_count == 1


def test_reused_extraction_reevaluates_contact_relationships(database) -> None:
    class ChangingContacts(ContactAwareAccountsClient):
        linked_account_id = "1661824839"

        def load_contacts_page(self, *args, **kwargs):
            page = super().load_contacts_page(*args, **kwargs)
            page["items"][0]["column_values"][1]["linked_item_ids"] = [
                self.linked_account_id
            ]
            return page

    content = eml_bytes(from_value="Sarah Morrissey <s.morrissey@encon.co.uk>")
    monday = FakeMonday(sales_item(identity(content)), content)
    model = FakePostcodeClient(requester_domain="encon.co.uk", company="Encon Group")
    accounts = ChangingContacts()
    deps = dependencies(monday, model, accounts)
    first = run_queued_enquiry(database, monday, deps)
    accounts.linked_account_id = "1661825541"
    second = run_queued_enquiry(database, monday, deps)

    assert first.result_json["account"]["accountItemId"] == "1661824839"
    assert second.result_json["account"]["accountItemId"] == "1661825541"
    assert second.result_json["reusedExtractionJobId"] == str(first.id)
    assert accounts.account_page_calls == accounts.contact_page_calls == 2
    assert model.extraction_count == monday.download_count == 1
    assert "s.morrissey@encon.co.uk" not in json.dumps(second.result_json)


@pytest.mark.parametrize("labels, expected_outcome, expected_label", [
    ([], "unmapped", None),
    ([{"id": 114, "label": "WA"}], "resolved", 114),
])
def test_reused_postcode_is_remapped_against_live_dropdown(
    database, completed_enquiry, labels, expected_outcome, expected_label,
) -> None:
    monday, model, _, deps, first = completed_enquiry
    monday.postcode_labels = labels
    second = run_queued_enquiry(database, monday, deps)

    assert second.result_json["reusedExtractionJobId"] == str(first.id)
    assert second.result_json["postcode"]["outcome"] == expected_outcome
    assert second.result_json["postcode"]["labelId"] == expected_label
    assert model.extraction_count == 1


@pytest.mark.parametrize("changed_identity", ["input", "pipeline"])
def test_changed_input_or_pipeline_requires_new_extraction(
    database, completed_enquiry, changed_identity,
) -> None:
    monday, model, _, deps, _ = completed_enquiry
    version = "test-v1"
    if changed_identity == "input":
        monday.item["assets"][0]["name"] = "renamed.eml"
    else:
        version = "test-v2"
    second = run_queued_enquiry(database, monday, deps, pipeline_version=version)

    assert "reusedExtractionJobId" not in second.result_json
    assert model.extraction_count == monday.download_count == 2


@pytest.mark.parametrize("settings_change", [
    {"internal_email_domains": ("taperedplus.co.uk", "internal.example")},
    {"internal_company_aliases": ("New Internal Company",)},
    {"requester_domain_aliases": {"relay.example": "company.example"}},
])
def test_changed_runtime_evidence_rules_invalidate_completed_extraction(
    database, completed_enquiry, settings_change,
) -> None:
    monday, model, _, deps, first = completed_enquiry
    second = run_queued_enquiry(database, monday, replace(deps, **settings_change))

    assert "reusedExtractionJobId" not in second.result_json
    assert second.result_json["extractionContextSha256"] != first.result_json["extractionContextSha256"]
    assert model.extraction_count == monday.download_count == 2


@pytest.mark.parametrize("checkpoint_change", [
    ("extractionContextSha256", None),
    ("schemaVersion", 2),
    ("schemaVersion", True),
    ("inputRevision", "f" * 64),
    ("pipelineVersion", "outdated"),
    ("completedStage", "extracting"),
    ("postcode", {}),
    ("requester", {}),
    ("postcode.assetIds", ["900"]),
    ("postcode.labelId", "invalid"),
    ("postcode.extractedTextSha256", "invalid"),
    ("requester.emailAddressSha256", "invalid"),
    ("requester.emailAddressSha256", None),
    ("requester.websiteDomains", [123]),
])
def test_legacy_or_incomplete_checkpoint_falls_back_to_full_extraction(
    database, completed_enquiry, checkpoint_change,
) -> None:
    monday, model, _, deps, first = completed_enquiry
    with database() as session:
        source = session.get(ProcessingJob, first.id)
        result = copy.deepcopy(source.result_json)
        path = checkpoint_change[0].split(".")
        target = result if len(path) == 1 else result[path[0]]
        target[path[-1]] = checkpoint_change[1]
        source.result_json = result
        # Python considers True equal to 1; still persist that damaged JSON type.
        flag_modified(source, "result_json")
        session.commit()
    second = run_queued_enquiry(database, monday, deps)

    assert "reusedExtractionJobId" not in second.result_json
    assert model.extraction_count == monday.download_count == 2


def test_checkpoint_reuse_does_not_copy_unrelated_saved_content(
    database, completed_enquiry,
) -> None:
    monday, _, _, deps, first = completed_enquiry
    with database() as session:
        source = session.get(ProcessingJob, first.id)
        result = copy.deepcopy(source.result_json)
        result["rawEmail"] = "PRIVATE-OLD-CONTENT"
        result["postcode"]["rawText"] = "PRIVATE-OLD-CONTENT"
        result["requester"]["emailAddress"] = "PRIVATE-OLD-CONTENT"
        source.result_json = result
        session.commit()
    second = run_queued_enquiry(database, monday, deps)

    assert second.result_json["reusedExtractionJobId"] == str(first.id)
    assert "PRIVATE-OLD-CONTENT" not in json.dumps(second.result_json)
    assert second.result_json["publication"] == {"outcome": "shadow_skipped"}


def test_force_reprocess_bypasses_completed_extraction(database, completed_enquiry) -> None:
    monday, model, _, deps, _ = completed_enquiry
    second = run_queued_enquiry(database, monday, deps, force_reprocess=True)

    assert "reusedExtractionJobId" not in second.result_json
    assert model.extraction_count == monday.download_count == 2
    with database() as session:
        intake_audit = session.query(ProcessingAudit).filter_by(
            job_id=second.id, event_type="webhook_intake"
        ).one()
        assert intake_audit.details_json["forceReprocess"] is True


@pytest.mark.parametrize("previous_outcome", ["not_found", "unmapped"])
def test_incomplete_postcode_outcomes_remain_reusable(database, previous_outcome) -> None:
    class NoPostcode(FakePostcodeClient):
        def extract_design_parameters(self, *args, **kwargs):
            super().extract_design_parameters(*args, **kwargs)
            return DesignParameterExtraction(post_code=None)

    content = eml_bytes()
    monday = PublishingMonday(sales_item(identity(content)), content)
    model = NoPostcode() if previous_outcome == "not_found" else FakePostcodeClient()
    monday.postcode_labels = []
    deps = dependencies(monday, model, EligibleAccountsClient())
    first = run_queued_enquiry(database, monday, deps)
    monday.postcode_labels = [{"id": 115, "label": "WA"}]
    second = run_queued_enquiry(database, monday, deps)

    assert first.result_json["postcode"]["outcome"] == previous_outcome
    assert second.result_json["reusedExtractionJobId"] == str(first.id)
    assert second.result_json["postcode"]["outcome"] == (
        "not_found" if previous_outcome == "not_found" else "resolved"
    )
    assert model.extraction_count == monday.download_count == 1


@pytest.mark.parametrize("change", ["input", "group", "board", "inactive"])
def test_reuse_rechecks_authoritative_eligibility_before_matching(
    database, completed_enquiry, change,
) -> None:
    monday, model, accounts, deps, _ = completed_enquiry
    deps = replace(deps, excluded_group_ids=("excluded",))
    with database() as session:
        queued = queue_sales_item_snapshot(
            session, parse_sales_item_snapshot(monday.item, contract=BOARD_CONTRACT),
            pipeline_version="test-v1", now=NOW + timedelta(seconds=1),
        )
        session.commit()
        claim_next_job(session, worker_id="worker-a", now=NOW + timedelta(seconds=1))
        session.commit()

    if change == "input":
        monday.item["assets"][0]["name"] = "changed.eml"
    elif change == "group":
        monday.item["group"]["id"] = "excluded"
        monday.item["column_values"] = []
    elif change == "board":
        monday.item["board"]["id"] = "1882196103"
        monday.item["column_values"] = []
    else:
        monday.item["state"] = "archived"

    outcome = run_pipeline_job(
        database, queued.job.id, worker_id="worker-a", dependencies=deps,
        mode="enabled", now=NOW + timedelta(seconds=1),
    )
    assert outcome == "superseded"
    assert model.extraction_count == monday.download_count == 1
    assert accounts.page_calls == 1
    assert len(monday.mutations) == 1
    with database() as session:
        assert session.get(ProcessingJob, queued.job.id).status == "cancelled"


@pytest.mark.parametrize("source_status", ["failed", "cancelled"])
def test_reuse_requires_a_completed_source(database, completed_enquiry, source_status) -> None:
    monday, model, _, deps, first = completed_enquiry
    with database() as session:
        session.get(ProcessingJob, first.id).status = source_status
        session.commit()
    second = run_queued_enquiry(database, monday, deps)

    assert "reusedExtractionJobId" not in second.result_json
    assert model.extraction_count == monday.download_count == 2


def test_force_reprocessing_survives_an_extraction_retry(
    database, completed_enquiry, monkeypatch,
) -> None:
    monday, model, _, deps, _ = completed_enquiry
    original_extract = model.extract_design_parameters
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("temporary extraction failure")
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(model, "extract_design_parameters", fail_once)
    with database() as session:
        queued = queue_sales_item_snapshot(
            session, parse_sales_item_snapshot(monday.item, contract=BOARD_CONTRACT),
            pipeline_version="test-v1", force_reprocess=True,
            now=NOW + timedelta(seconds=1),
        )
        session.commit()
        claim_next_job(session, worker_id="worker-a", now=NOW + timedelta(seconds=1))
        session.commit()
    with pytest.raises(TimeoutError):
        run_pipeline_job(
            database, queued.job.id, worker_id="worker-a", dependencies=deps,
            mode="shadow", now=NOW + timedelta(seconds=1),
        )
    with database() as session:
        failed = session.get(ProcessingJob, queued.job.id)
        assert failed.result_json == {"forceReprocess": True}
        retry_or_fail_job(
            session, failed.id, worker_id="worker-a", error=TimeoutError(), now=NOW,
        )
        retry_at = failed.next_retry_at
        session.commit()
        claim_next_job(session, worker_id="worker-a", now=retry_at)
        session.commit()
    run_pipeline_job(
        database, queued.job.id, worker_id="worker-a", dependencies=deps,
        mode="shadow", now=retry_at,
    )
    assert calls == 2
    assert monday.download_count == 3
    with database() as session:
        completed = session.get(ProcessingJob, queued.job.id)
        assert completed.status == "completed"
        assert "reusedExtractionJobId" not in completed.result_json
