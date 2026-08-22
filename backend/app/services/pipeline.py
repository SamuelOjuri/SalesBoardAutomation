"""Resumable analysis and publication checkpoints for processing jobs."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol, cast

from sqlalchemy.orm import Session, sessionmaker

from app.config import BOARD_CONTRACT
from app.input_revision import build_input_manifest, compute_input_revision
from app.models import (
    ProcessingAudit,
    ProcessingItem,
    ProcessingItemState,
    ProcessingJob,
    ProcessingJobStage,
    ProcessingJobStatus,
)
from app.publication_gate import PublicationGate
from app.services.accounts import (
    AccountMatchResult,
    AccountsIndexService,
    match_account,
)
from app.services.email_parser import ParsedEmail, process_email_content
from app.services.intake import (
    AssetDownloader,
    DownloadedEmailAsset,
    SalesItemSnapshot,
    download_email_assets,
    is_excluded_sales_group,
    parse_sales_item_snapshot,
    queue_sales_item_snapshot,
)
from app.services.postcode import (
    PostcodeAnalysisResult,
    PostcodeExtractionClient,
    analyze_downloaded_email_assets,
)
from app.services.publication import (
    PublicationResult,
    SalesPublicationClient,
    StalePublicationError,
    publish_sales_item,
)
from app.services.requester_identity import (
    RequesterIdentity,
    RequesterSource,
    extract_requester_identity,
)
from app.services.worker import complete_job, lock_owned_job, utc_now


ProcessingMode = Literal["off", "shadow", "allowlist", "enabled"]
PipelineOutcome = Literal["completed", "shadow_completed", "superseded"]


class PipelineExecutionDisabled(RuntimeError):
    """Raised when the current rollout mode does not permit analysis."""


class PipelineMondayClient(AssetDownloader, SalesPublicationClient, Protocol):
    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]: ...

    def load_postcode_dropdown_column(
        self, board_id: int
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    monday: PipelineMondayClient
    postcode_client: PostcodeExtractionClient
    accounts: AccountsIndexService
    publication_gate: PublicationGate
    internal_email_domains: tuple[str, ...]
    excluded_group_ids: tuple[str, ...] = ()
    allow_name_fallback: bool = False
    internal_company_aliases: tuple[str, ...] = ()
    requester_domain_aliases: Mapping[str, str] = field(default_factory=dict)
    account_requester_domain_aliases: Mapping[str, Sequence[str]] = field(
        default_factory=dict
    )


def analysis_allowed(
    mode: ProcessingMode,
    item_id: str,
    allowlist_item_ids: Sequence[str] = (),
) -> bool:
    if mode in {"shadow", "enabled"}:
        return True
    if mode == "allowlist":
        return str(item_id) in {str(value) for value in allowlist_item_ids}
    return False


def publication_allowed(
    mode: ProcessingMode,
    item_id: str,
    allowlist_item_ids: Sequence[str] = (),
) -> bool:
    if mode == "enabled":
        return True
    if mode == "allowlist":
        return str(item_id) in {str(value) for value in allowlist_item_ids}
    return False


def run_pipeline_job(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    *,
    worker_id: str,
    dependencies: PipelineDependencies,
    mode: ProcessingMode,
    allowlist_item_ids: Sequence[str] = (),
    now: datetime | None = None,
) -> PipelineOutcome:
    """Run remaining stages and commit every completed stage independently."""

    checkpoint_time = now or utc_now()
    while True:
        with session_factory() as session:
            job = lock_owned_job(session, job_id, worker_id=worker_id)
            if not analysis_allowed(mode, job.item_id, allowlist_item_ids):
                raise PipelineExecutionDisabled(
                    "processing mode does not permit this Sales item"
                )
            stage = job.stage
            session.commit()

        if stage == ProcessingJobStage.EXTRACTING.value:
            if not _run_extraction_stage(
                session_factory,
                job_id,
                worker_id=worker_id,
                dependencies=dependencies,
                now=checkpoint_time,
            ):
                return "superseded"
            continue
        if stage == ProcessingJobStage.MATCHING_ACCOUNT.value:
            if not _run_matching_stage(
                session_factory,
                job_id,
                worker_id=worker_id,
                dependencies=dependencies,
                now=checkpoint_time,
            ):
                return "superseded"
            continue
        if stage == ProcessingJobStage.VALIDATING.value:
            validation = _run_validation_stage(
                session_factory,
                job_id,
                worker_id=worker_id,
                dependencies=dependencies,
                publish=publication_allowed(
                    mode,
                    _job_item_id(session_factory, job_id),
                    allowlist_item_ids,
                ),
                now=checkpoint_time,
            )
            if validation == "superseded":
                return "superseded"
            if validation == "shadow_completed":
                return "shadow_completed"
            continue
        if stage == ProcessingJobStage.PUBLISHING.value:
            try:
                _run_publication_stage(
                    session_factory,
                    job_id,
                    worker_id=worker_id,
                    dependencies=dependencies,
                    now=checkpoint_time,
                )
            except StalePublicationError:
                _replace_with_authoritative_input(
                    session_factory,
                    job_id,
                    worker_id=worker_id,
                    dependencies=dependencies,
                    now=checkpoint_time,
                )
                return "superseded"
            return "completed"
        raise RuntimeError(f"processing job has unsupported stage {stage!r}")


def _run_extraction_stage(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    *,
    worker_id: str,
    dependencies: PipelineDependencies,
    now: datetime,
) -> bool:
    job = _read_owned_job(session_factory, job_id, worker_id=worker_id)
    snapshot = _load_snapshot(dependencies.monday, job.item_id)
    if not _snapshot_matches_job(
        snapshot,
        job,
        dependencies.excluded_group_ids,
    ):
        _replace_with_snapshot(
            session_factory,
            job_id,
            worker_id=worker_id,
            snapshot=snapshot,
            excluded_group_ids=dependencies.excluded_group_ids,
            now=now,
        )
        return False

    postcode_column = dependencies.monday.load_postcode_dropdown_column(
        BOARD_CONTRACT.sales_board_id
    )
    with download_email_assets(dependencies.monday, snapshot.email_assets) as assets:
        requester = _extract_requester(
            assets,
            internal_domains=dependencies.internal_email_domains,
            domain_aliases=dependencies.requester_domain_aliases,
        )
        postcode = analyze_downloaded_email_assets(
            assets,
            client=dependencies.postcode_client,
            postcode_column=postcode_column,
            requester=requester,
            internal_company_aliases=dependencies.internal_company_aliases,
        )
        requester = replace(requester, company=postcode.company)

    with session_factory() as session:
        current = lock_owned_job(session, job_id, worker_id=worker_id)
        item = _lock_item(session, current)
        if _database_identity_changed(item, current):
            session.rollback()
            _replace_with_authoritative_input(
                session_factory,
                job_id,
                worker_id=worker_id,
                dependencies=dependencies,
                now=now,
            )
            return False
        postcode_payload = _postcode_payload(postcode)
        result = _identity_payload(current)
        result.update(
            {
                "completedStage": ProcessingJobStage.EXTRACTING.value,
                "postcode": postcode_payload,
                "requester": _requester_payload(requester),
            }
        )
        current.result_json = result
        current.stage = ProcessingJobStage.MATCHING_ACCOUNT.value
        current.heartbeat_at = now
        item.postcode_result_json = postcode_payload
        item.account_match_json = None
        item.state = ProcessingItemState.PROCESSING.value
        _add_stage_audit(session, item, current, "completed")
        session.commit()
    return True


def _run_matching_stage(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    *,
    worker_id: str,
    dependencies: PipelineDependencies,
    now: datetime,
) -> bool:
    job = _read_owned_job(session_factory, job_id, worker_id=worker_id)
    snapshot = _load_snapshot(dependencies.monday, job.item_id)
    if not _snapshot_matches_job(
        snapshot,
        job,
        dependencies.excluded_group_ids,
    ):
        _replace_with_snapshot(
            session_factory,
            job_id,
            worker_id=worker_id,
            snapshot=snapshot,
            excluded_group_ids=dependencies.excluded_group_ids,
            now=now,
        )
        return False
    result = _current_result(job, ProcessingJobStage.EXTRACTING.value)
    requester = _requester_from_payload(result.get("requester"))
    account_match = match_account(
        dependencies.accounts.load_index(),
        requester,
        allow_name_fallback=dependencies.allow_name_fallback,
        account_domain_aliases=dependencies.account_requester_domain_aliases,
    )

    with session_factory() as session:
        current = lock_owned_job(session, job_id, worker_id=worker_id)
        item = _lock_item(session, current)
        if _database_identity_changed(item, current):
            session.rollback()
            _replace_with_authoritative_input(
                session_factory,
                job_id,
                worker_id=worker_id,
                dependencies=dependencies,
                now=now,
            )
            return False
        checkpoint = _current_result(
            current, ProcessingJobStage.EXTRACTING.value
        )
        account_payload = _account_payload(account_match)
        checkpoint["account"] = account_payload
        checkpoint["completedStage"] = ProcessingJobStage.MATCHING_ACCOUNT.value
        current.result_json = checkpoint
        current.stage = ProcessingJobStage.VALIDATING.value
        current.heartbeat_at = now
        item.account_match_json = account_payload
        item.state = ProcessingItemState.ANALYZED.value
        _add_stage_audit(session, item, current, "completed")
        session.commit()
    return True


def _run_validation_stage(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    *,
    worker_id: str,
    dependencies: PipelineDependencies,
    publish: bool,
    now: datetime,
) -> Literal["publishing", "shadow_completed", "superseded"]:
    job = _read_owned_job(session_factory, job_id, worker_id=worker_id)
    snapshot = _load_snapshot(dependencies.monday, job.item_id)
    if not _snapshot_matches_job(
        snapshot,
        job,
        dependencies.excluded_group_ids,
    ):
        _replace_with_snapshot(
            session_factory,
            job_id,
            worker_id=worker_id,
            snapshot=snapshot,
            excluded_group_ids=dependencies.excluded_group_ids,
            now=now,
        )
        return "superseded"

    with session_factory() as session:
        current = lock_owned_job(session, job_id, worker_id=worker_id)
        item = _lock_item(session, current)
        if _database_identity_changed(item, current):
            session.rollback()
            _replace_with_authoritative_input(
                session_factory,
                job_id,
                worker_id=worker_id,
                dependencies=dependencies,
                now=now,
            )
            return "superseded"
        checkpoint = _current_result(
            current, ProcessingJobStage.MATCHING_ACCOUNT.value
        )
        checkpoint["completedStage"] = ProcessingJobStage.VALIDATING.value
        if not publish:
            checkpoint["publication"] = {"outcome": "shadow_skipped"}
            complete_job(
                session,
                job_id,
                worker_id=worker_id,
                result=checkpoint,
                now=now,
            )
            _add_stage_audit(session, item, current, "shadow_skipped")
            session.commit()
            return "shadow_completed"

        current.result_json = checkpoint
        current.stage = ProcessingJobStage.PUBLISHING.value
        current.heartbeat_at = now
        item.state = ProcessingItemState.PUBLISHING.value
        _add_stage_audit(session, item, current, "completed")
        session.commit()
    return "publishing"


def _run_publication_stage(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    *,
    worker_id: str,
    dependencies: PipelineDependencies,
    now: datetime,
) -> None:
    job = _read_owned_job(session_factory, job_id, worker_id=worker_id)
    result = _current_result(job, ProcessingJobStage.VALIDATING.value)
    postcode = _mapping(result.get("postcode"), "postcode checkpoint")
    account = _mapping(result.get("account"), "account checkpoint")
    account_item_id = account.get("accountItemId")
    publication = publish_sales_item(
        client=dependencies.monday,
        publication_gate=dependencies.publication_gate,
        item_id=job.item_id,
        input_revision=job.input_revision,
        postcode_label_id=_optional_int(postcode.get("labelId")),
        account_item_id=(
            str(account_item_id) if account_item_id is not None else None
        ),
        accounts=dependencies.accounts,
        excluded_group_ids=dependencies.excluded_group_ids,
    )

    with session_factory() as session:
        current = lock_owned_job(session, job_id, worker_id=worker_id)
        item = _lock_item(session, current)
        if _database_identity_changed(item, current):
            raise StalePublicationError(
                "processing input changed before publication checkpoint"
            )
        checkpoint = _current_result(current, ProcessingJobStage.VALIDATING.value)
        checkpoint["completedStage"] = ProcessingJobStage.PUBLISHING.value
        checkpoint["publication"] = _publication_payload(publication)
        current.result_json = checkpoint
        current.heartbeat_at = now
        _add_stage_audit(session, item, current, "completed")
        complete_job(
            session,
            job_id,
            worker_id=worker_id,
            result=checkpoint,
            now=now,
        )
        session.commit()


def _replace_with_authoritative_input(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    *,
    worker_id: str,
    dependencies: PipelineDependencies,
    now: datetime,
) -> None:
    job = _read_owned_job(session_factory, job_id, worker_id=worker_id)
    snapshot = _load_snapshot(dependencies.monday, job.item_id)
    _replace_with_snapshot(
        session_factory,
        job_id,
        worker_id=worker_id,
        snapshot=snapshot,
        excluded_group_ids=dependencies.excluded_group_ids,
        now=now,
    )


def _replace_with_snapshot(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    *,
    worker_id: str,
    snapshot: SalesItemSnapshot,
    excluded_group_ids: Sequence[str],
    now: datetime,
) -> None:
    with session_factory() as session:
        job = lock_owned_job(session, job_id, worker_id=worker_id)
        item = _lock_item(session, job)
        group_excluded = is_excluded_sales_group(
            snapshot.group_id,
            excluded_group_ids,
        )
        replacement_revision = (
            compute_input_revision(asset.identity for asset in snapshot.email_assets)
            if snapshot.active and snapshot.email_assets and not group_excluded
            else None
        )
        job.status = ProcessingJobStatus.CANCELLED.value
        job.completed_at = now
        job.superseded_by_revision = replacement_revision
        job.last_error = "GroupExcluded" if group_excluded else "InputSuperseded"
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        _add_audit(
            session,
            item,
            job,
            event_type=("group_exclusion" if group_excluded else "input_supersession"),
            outcome=("excluded_group" if group_excluded else "cancelled"),
            details={
                "groupId": snapshot.group_id,
                "supersededByRevision": replacement_revision,
            },
        )
        if replacement_revision is None:
            item.latest_input_revision = None
            item.latest_pipeline_version = None
            item.supersession_requested_at = None
            item.state = ProcessingItemState.INELIGIBLE.value
        else:
            item.latest_input_revision = replacement_revision
            item.latest_pipeline_version = job.pipeline_version
            item.supersession_requested_at = now
            queue_sales_item_snapshot(
                session,
                snapshot,
                pipeline_version=job.pipeline_version,
                trigger_type="input_supersession",
                excluded_group_ids=excluded_group_ids,
                now=now,
            )
        session.commit()


def _load_snapshot(
    monday: PipelineMondayClient,
    item_id: str,
) -> SalesItemSnapshot:
    return parse_sales_item_snapshot(
        monday.load_sales_item_intake(item_id),
        contract=BOARD_CONTRACT,
    )


def _snapshot_matches_job(
    snapshot: SalesItemSnapshot,
    job: ProcessingJob,
    excluded_group_ids: Sequence[str] = (),
) -> bool:
    if (
        not snapshot.active
        or is_excluded_sales_group(snapshot.group_id, excluded_group_ids)
        or snapshot.board_id != job.board_id
        or snapshot.item_id != job.item_id
        or not snapshot.email_assets
    ):
        return False
    identities = tuple(asset.identity for asset in snapshot.email_assets)
    return (
        compute_input_revision(identities) == job.input_revision
        and build_input_manifest(identities) == job.input_manifest_json
    )


def _read_owned_job(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    *,
    worker_id: str,
) -> ProcessingJob:
    with session_factory() as session:
        job = lock_owned_job(session, job_id, worker_id=worker_id)
        session.expunge(job)
        session.commit()
        return job


def _job_item_id(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
) -> str:
    with session_factory() as session:
        job = session.get(ProcessingJob, job_id)
        if job is None:
            raise RuntimeError("processing job no longer exists")
        return job.item_id


def _lock_item(session: Session, job: ProcessingJob) -> ProcessingItem:
    query = session.query(ProcessingItem).filter_by(
        board_id=job.board_id,
        item_id=job.item_id,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.one()


def _database_identity_changed(
    item: ProcessingItem,
    job: ProcessingJob,
) -> bool:
    return (
        item.latest_input_revision != job.input_revision
        or item.latest_pipeline_version != job.pipeline_version
    )


def _current_result(
    job: ProcessingJob,
    required_completed_stage: str,
) -> dict[str, Any]:
    result = job.result_json
    if (
        not isinstance(result, Mapping)
        or result.get("schemaVersion") != 1
        or result.get("inputRevision") != job.input_revision
        or result.get("pipelineVersion") != job.pipeline_version
        or result.get("completedStage") != required_completed_stage
    ):
        raise RuntimeError("processing job checkpoint is missing or stale")
    return dict(result)


def _identity_payload(job: ProcessingJob) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "inputRevision": job.input_revision,
        "pipelineVersion": job.pipeline_version,
    }


def _extract_requester(
    assets: Sequence[DownloadedEmailAsset],
    *,
    internal_domains: tuple[str, ...],
    domain_aliases: Mapping[str, str],
) -> RequesterIdentity:
    fallback: RequesterIdentity | None = None
    for asset in sorted(assets, key=lambda value: int(value.identity.asset_id)):
        parsed: ParsedEmail = process_email_content(
            asset.path.read_bytes(), asset.identity.filename
        )
        requester = extract_requester_identity(
            parsed,
            internal_domains=internal_domains,
            domain_aliases=domain_aliases,
        )
        fallback = fallback or requester
        if requester.source != "not_found":
            return requester
    if fallback is not None:
        return fallback
    raise RuntimeError("downloaded Email assets are unavailable")


def _postcode_payload(result: PostcodeAnalysisResult) -> dict[str, Any]:
    return {
        "outcome": result.outcome,
        "area": result.area,
        "labelId": result.label_id,
        "assetIds": list(result.asset_ids),
        "extractedTextSha256": result.extracted_text_sha256,
        "company": result.company,
    }


def _requester_payload(requester: RequesterIdentity) -> dict[str, Any]:
    return {
        "domain": requester.domain,
        "company": requester.company,
        "source": requester.source,
    }


def _requester_from_payload(value: object) -> RequesterIdentity:
    payload = _mapping(value, "requester checkpoint")
    source = str(payload.get("source"))
    if source not in {"top_level_sender", "forwarded_sender", "not_found"}:
        raise RuntimeError("requester checkpoint source is invalid")
    domain = payload.get("domain")
    company = payload.get("company")
    return RequesterIdentity(
        email_address=None,
        domain=str(domain) if domain is not None else None,
        company=str(company) if company is not None else None,
        source=cast(RequesterSource, source),
    )


def _account_payload(result: AccountMatchResult) -> dict[str, Any]:
    return {
        "resolution": str(result.resolution),
        "reason": result.reason,
        "accountItemId": result.account.item_id if result.account else None,
        "domainCandidateIds": list(result.domain_candidate_ids),
        "nameCandidateIds": list(result.name_candidate_ids),
    }


def _publication_payload(result: PublicationResult) -> dict[str, Any]:
    return {
        "outcome": "published" if result.mutation_attempted else "no_write_needed",
        "mutationAttempted": result.mutation_attempted,
        "mutationWasAmbiguous": result.mutation_was_ambiguous,
        "postcode": {
            "outcome": result.postcode.outcome,
            "intendedId": result.postcode.intended_id,
            "existingIds": list(result.postcode.existing_ids),
        },
        "accounts": {
            "outcome": result.accounts.outcome,
            "intendedId": result.accounts.intended_id,
            "existingIds": list(result.accounts.existing_ids),
        },
    }


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{name} is missing")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeError("postcode checkpoint label ID is invalid")
    try:
        identifier = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("postcode checkpoint label ID is invalid") from error
    if identifier <= 0:
        raise RuntimeError("postcode checkpoint label ID is invalid")
    return identifier


def _add_stage_audit(
    session: Session,
    item: ProcessingItem,
    job: ProcessingJob,
    outcome: str,
) -> None:
    _add_audit(
        session,
        item,
        job,
        event_type="pipeline_stage",
        outcome=outcome,
        details={"completedStage": job.result_json.get("completedStage") if job.result_json else None},
    )


def _add_audit(
    session: Session,
    item: ProcessingItem,
    job: ProcessingJob,
    *,
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
