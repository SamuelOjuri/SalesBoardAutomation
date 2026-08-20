"""Authoritative Sales item and Email File intake parsing."""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from tempfile import TemporaryDirectory
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import BoardContract
from app.input_revision import (
    EmailAssetIdentity,
    build_input_manifest,
    compute_input_revision,
)
from app.models import (
    ACTIVE_JOB_STATUSES,
    ProcessingAudit,
    ProcessingItem,
    ProcessingItemState,
    ProcessingJob,
    ProcessingJobStage,
    ProcessingJobStatus,
)


SUPPORTED_EMAIL_EXTENSIONS = frozenset({".eml", ".msg"})


class IntakeContractError(ValueError):
    """Raised when Monday returns an unsafe or incomplete intake snapshot."""


@dataclass(frozen=True, slots=True)
class EmailAsset:
    identity: EmailAssetIdentity
    download_url: str


@dataclass(frozen=True, slots=True)
class SalesItemSnapshot:
    board_id: str
    item_id: str
    active: bool
    email_assets: tuple[EmailAsset, ...]


@dataclass(frozen=True, slots=True)
class DownloadedEmailAsset:
    identity: EmailAssetIdentity
    path: Path
    sha256: str


class AssetDownloader(Protocol):
    def download_asset(
        self,
        url: str,
        destination: Path,
        *,
        expected_size: int,
        expected_sha256: str | None = None,
    ) -> str: ...


QueueOutcome = Literal["queued", "coalesced"]


@dataclass(frozen=True, slots=True)
class IntakeQueueResult:
    item: ProcessingItem
    job: ProcessingJob
    outcome: QueueOutcome
    created_job: bool


def parse_sales_item_snapshot(
    raw_item: Mapping[str, Any],
    *,
    contract: BoardContract,
) -> SalesItemSnapshot:
    item_id = _positive_decimal_id(raw_item.get("id"), "item ID")
    board = _mapping(raw_item.get("board"), "item board")
    board_id = _positive_decimal_id(board.get("id"), "board ID")
    state = raw_item.get("state")
    if not isinstance(state, str):
        raise IntakeContractError("Monday item state is missing")

    columns = raw_item.get("column_values")
    if not isinstance(columns, list):
        raise IntakeContractError("Monday item columns are missing")
    matching_columns = [
        value
        for value in columns
        if isinstance(value, Mapping)
        and str(value.get("id")) == contract.email_file_column_id
    ]
    if len(matching_columns) != 1:
        raise IntakeContractError("Monday Email File column is missing or duplicated")
    email_column = matching_columns[0]
    if email_column.get("type") != "file":
        raise IntakeContractError("Monday Email File column has the wrong type")

    metadata_by_id = _asset_metadata_by_id(raw_item.get("assets"))
    email_assets: list[EmailAsset] = []
    seen_asset_ids: set[str] = set()
    for member in _file_members(email_column.get("value")):
        asset_id = _positive_decimal_id(member.get("assetId"), "asset ID")
        if asset_id in seen_asset_ids:
            raise IntakeContractError(f"Email File contains duplicate asset {asset_id}")
        seen_asset_ids.add(asset_id)
        metadata = metadata_by_id.get(asset_id)
        if metadata is None:
            raise IntakeContractError(
                f"Email File asset {asset_id} is missing metadata"
            )
        filename = metadata.get("name")
        if not isinstance(filename, str) or not filename.strip():
            raise IntakeContractError(f"Email File asset {asset_id} has no filename")
        if PurePath(filename).suffix.casefold() not in SUPPORTED_EMAIL_EXTENSIONS:
            continue

        size_bytes = _nonnegative_int(metadata.get("file_size"), "asset size")
        created_at = _timestamp(metadata.get("created_at"))
        download_url = _download_url(metadata)
        email_assets.append(
            EmailAsset(
                identity=EmailAssetIdentity(
                    asset_id=asset_id,
                    filename=filename,
                    size_bytes=size_bytes,
                    created_at=created_at,
                ),
                download_url=download_url,
            )
        )

    return SalesItemSnapshot(
        board_id=board_id,
        item_id=item_id,
        active=state.casefold() == "active",
        email_assets=tuple(
            sorted(email_assets, key=lambda asset: int(asset.identity.asset_id))
        ),
    )


def queue_sales_item_snapshot(
    session: Session,
    snapshot: SalesItemSnapshot,
    *,
    webhook_event_id: uuid.UUID,
    pipeline_version: str,
    now: datetime | None = None,
) -> IntakeQueueResult:
    if not snapshot.active:
        raise ValueError("only active Sales items can be queued")
    if not snapshot.email_assets:
        raise ValueError("at least one supported email asset is required")
    now = now or datetime.now(timezone.utc)
    identities = tuple(asset.identity for asset in snapshot.email_assets)
    manifest = build_input_manifest(identities)
    revision = compute_input_revision(identities)

    item = _processing_item_query(
        session, board_id=snapshot.board_id, item_id=snapshot.item_id
    ).one_or_none()
    if item is None:
        candidate = ProcessingItem(
            board_id=snapshot.board_id,
            item_id=snapshot.item_id,
            state=ProcessingItemState.SCHEDULED.value,
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush([candidate])
            item = candidate
        except IntegrityError:
            item = _processing_item_query(
                session, board_id=snapshot.board_id, item_id=snapshot.item_id
            ).one()

    item.latest_input_revision = revision
    item.latest_pipeline_version = pipeline_version
    active_job = _active_job_query(
        session, board_id=snapshot.board_id, item_id=snapshot.item_id
    ).first()
    if active_job is not None:
        if (
            active_job.input_revision != revision
            or active_job.pipeline_version != pipeline_version
        ):
            item.supersession_requested_at = now
        item.state = (
            ProcessingItemState.PROCESSING.value
            if active_job.status == ProcessingJobStatus.RUNNING.value
            else ProcessingItemState.SCHEDULED.value
        )
        _add_intake_audit(
            session,
            item=item,
            job=active_job,
            webhook_event_id=webhook_event_id,
            outcome="coalesced",
            input_revision=revision,
            pipeline_version=pipeline_version,
        )
        return IntakeQueueResult(item, active_job, "coalesced", False)

    candidate_job = ProcessingJob(
        board_id=snapshot.board_id,
        item_id=snapshot.item_id,
        trigger_type="webhook_email",
        input_revision=revision,
        input_manifest_json=manifest,
        pipeline_version=pipeline_version,
        status=ProcessingJobStatus.SCHEDULED.value,
        stage=ProcessingJobStage.EXTRACTING.value,
        scheduled_for=now,
    )
    try:
        with session.begin_nested():
            session.add(candidate_job)
            session.flush([candidate_job])
        job = candidate_job
        outcome: QueueOutcome = "queued"
        created_job = True
    except IntegrityError:
        job = _active_job_query(
            session, board_id=snapshot.board_id, item_id=snapshot.item_id
        ).one()
        outcome = "coalesced"
        created_job = False

    item.state = ProcessingItemState.SCHEDULED.value
    _add_intake_audit(
        session,
        item=item,
        job=job,
        webhook_event_id=webhook_event_id,
        outcome=outcome,
        input_revision=revision,
        pipeline_version=pipeline_version,
    )
    return IntakeQueueResult(item, job, outcome, created_job)


@contextmanager
def download_email_assets(
    downloader: AssetDownloader,
    assets: Sequence[EmailAsset],
) -> Generator[tuple[DownloadedEmailAsset, ...]]:
    with TemporaryDirectory(prefix="sales-email-") as temporary_directory:
        root = Path(temporary_directory)
        downloaded: list[DownloadedEmailAsset] = []
        for asset in sorted(assets, key=lambda value: int(value.identity.asset_id)):
            suffix = PurePath(asset.identity.filename).suffix.casefold()
            destination = root / f"{asset.identity.asset_id}{suffix}"
            sha256 = downloader.download_asset(
                asset.download_url,
                destination,
                expected_size=asset.identity.size_bytes,
            )
            if len(sha256) != 64 or any(
                character not in "0123456789abcdef" for character in sha256
            ):
                raise IntakeContractError(
                    "asset downloader returned an invalid SHA-256"
                )
            downloaded.append(
                DownloadedEmailAsset(
                    identity=asset.identity,
                    path=destination,
                    sha256=sha256,
                )
            )
        yield tuple(downloaded)


def _processing_item_query(
    session: Session, *, board_id: str, item_id: str
):
    query = session.query(ProcessingItem).filter_by(
        board_id=board_id, item_id=item_id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query


def _active_job_query(session: Session, *, board_id: str, item_id: str):
    query = (
        session.query(ProcessingJob)
        .filter(
            ProcessingJob.board_id == board_id,
            ProcessingJob.item_id == item_id,
            ProcessingJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(ProcessingJob.created_at, ProcessingJob.id)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query


def _add_intake_audit(
    session: Session,
    *,
    item: ProcessingItem,
    job: ProcessingJob,
    webhook_event_id: uuid.UUID,
    outcome: str,
    input_revision: str,
    pipeline_version: str,
) -> None:
    session.add(
        ProcessingAudit(
            board_id=item.board_id,
            item_id=item.item_id,
            job_id=job.id,
            webhook_event_id=webhook_event_id,
            event_type="webhook_intake",
            stage=ProcessingJobStage.EXTRACTING.value,
            outcome=outcome,
            input_revision=input_revision,
            pipeline_version=pipeline_version,
            details_json={},
        )
    )


def _asset_metadata_by_id(raw_assets: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(raw_assets, list):
        raise IntakeContractError("Monday item assets are missing")
    result: dict[str, Mapping[str, Any]] = {}
    for raw_asset in raw_assets:
        metadata = _mapping(raw_asset, "asset metadata")
        asset_id = _positive_decimal_id(metadata.get("id"), "asset ID")
        if asset_id in result:
            raise IntakeContractError(f"Monday returned duplicate asset {asset_id}")
        result[asset_id] = metadata
    return result


def _file_members(raw_value: object) -> list[Mapping[str, Any]]:
    if raw_value is None or raw_value == "":
        return []
    if not isinstance(raw_value, str):
        raise IntakeContractError("Monday Email File value is malformed")
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise IntakeContractError("Monday Email File value is malformed") from error
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("files"), list):
        raise IntakeContractError("Monday Email File membership is malformed")
    members = parsed["files"]
    if not all(isinstance(member, Mapping) for member in members):
        raise IntakeContractError("Monday Email File membership is malformed")
    return members


def _positive_decimal_id(value: object, field_name: str) -> str:
    if value is None or isinstance(value, bool):
        raise IntakeContractError(f"Monday {field_name} is missing")
    normalized = str(value).strip()
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise IntakeContractError(f"Monday {field_name} is malformed")
    return str(int(normalized))


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntakeContractError(f"Monday {field_name} is malformed")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if value is None or isinstance(value, bool):
        raise IntakeContractError(f"Monday {field_name} is missing")
    normalized = str(value).strip()
    if not normalized.isdecimal():
        raise IntakeContractError(f"Monday {field_name} is malformed")
    return int(normalized)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise IntakeContractError("Monday asset creation timestamp is missing")
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise IntakeContractError(
            "Monday asset creation timestamp is malformed"
        ) from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise IntakeContractError("Monday asset creation timestamp has no timezone")
    return timestamp


def _download_url(metadata: Mapping[str, Any]) -> str:
    value = metadata.get("url") or metadata.get("public_url")
    if not isinstance(value, str):
        raise IntakeContractError("Monday asset download URL is missing")
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise IntakeContractError("Monday asset download URL must use HTTPS")
    return value