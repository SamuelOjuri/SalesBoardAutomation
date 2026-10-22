"""Coalesce durable webhook notifications before authoritative Monday reads.

The transaction owns both the item advisory lock and the selected event rows.
A crash rolls back queue changes and leaves every event eligible for recovery.
Notifications accepted during the read are deliberately left for the next pass.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import WebhookEvent, WebhookEventStatus
from app.services.intake import (
    IntakeContractError, is_excluded_sales_group,
    parse_sales_item_snapshot, queue_sales_item_snapshot,
)
from app.services.usage import record_usage
from app.services.worker import monday_failure, retry_delay_seconds, safe_error_details, utc_now

MAX_INTAKE_ATTEMPTS = 3


class IntakeReader(Protocol):
    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]: ...


def _due(now: datetime):
    return or_(
        and_(WebhookEvent.status == WebhookEventStatus.RECEIVED.value,
             or_(WebhookEvent.next_retry_at.is_(None), WebhookEvent.next_retry_at <= now)),
        and_(WebhookEvent.status == WebhookEventStatus.FAILED.value,
             WebhookEvent.next_retry_at <= now),
        # Recover records left by the former synchronous endpoint at rollout.
        and_(WebhookEvent.status == WebhookEventStatus.PROCESSING.value,
             or_(WebhookEvent.processing_started_at.is_(None),
                 WebhookEvent.processing_started_at <= now - timedelta(minutes=5))),
    )


def process_next_webhook_intake(
    session_factory: sessionmaker[Session], client: IntakeReader, *, settings: Settings,
    now: datetime | None = None,
) -> bool:
    processed_at = now or utc_now()
    with session_factory.begin() as session:
        candidate = session.scalars(
            select(WebhookEvent)
            .where(_due(processed_at), WebhookEvent.authenticated.is_(True),
                   WebhookEvent.board_id == str(settings.sales_board_id),
                   WebhookEvent.column_id == settings.board_contract.email_file_column_id,
                   WebhookEvent.item_id.is_not(None))
            .order_by(WebhookEvent.received_at, WebhookEvent.id)
            .limit(1).with_for_update(skip_locked=True)
        ).first()
        if candidate is None:
            return False
        if session.get_bind().dialect.name == "postgresql":
            lock_key = int.from_bytes(hashlib.blake2b(
                f"webhook-intake:{candidate.board_id}:{candidate.item_id}".encode(),
                digest_size=8,
            ).digest(), "big", signed=True)
            if not session.scalar(select(func.pg_try_advisory_xact_lock(lock_key))):
                return False
        events = list(session.scalars(
            select(WebhookEvent).where(
                _due(processed_at), WebhookEvent.authenticated.is_(True),
                WebhookEvent.board_id == candidate.board_id,
                WebhookEvent.item_id == candidate.item_id,
                WebhookEvent.column_id == settings.board_contract.email_file_column_id,
            ).order_by(WebhookEvent.received_at, WebhookEvent.id)
            .with_for_update()
        ))
        try:
            # Roll back all queue/audit changes on failure, retaining ownership
            # of the selected event rows for durable retry accounting below.
            with session.begin_nested():
                raw = client.load_sales_item_intake(candidate.item_id)
                snapshot = parse_sales_item_snapshot(
                    raw, contract=settings.board_contract, require_download_urls=False,
                    excluded_group_ids=settings.processing_excluded_group_ids,
                )
                if snapshot.item_id != candidate.item_id:
                    raise IntakeContractError("Monday returned the wrong Sales item")
                reason = (
                    "board_not_managed" if snapshot.board_id != str(settings.sales_board_id)
                    else "group_excluded" if is_excluded_sales_group(
                        snapshot.group_id, settings.processing_excluded_group_ids)
                    else "item_not_active" if not snapshot.active
                    else "no_supported_email" if not snapshot.email_assets
                    else None
                )
                for event in events:
                    if reason is None:
                        queue_sales_item_snapshot(
                            session, snapshot, webhook_event_id=event.id,
                            pipeline_version=settings.processing_pipeline_version,
                            excluded_group_ids=settings.processing_excluded_group_ids,
                            now=processed_at,
                        )
                    event.attempt_count += 1
                    event.status = WebhookEventStatus.PROCESSED.value
                    event.processing_started_at = None
                    event.processed_at = processed_at
                    event.next_retry_at = None
                    event.error = reason
                record_usage(session, "webhook_intake", "batches")
                record_usage(session, "webhook_intake", "events", len(events))
        except Exception as error:
            upstream = monday_failure(error)
            retryable = upstream.retryable if upstream is not None else getattr(
                error, "retryable", not isinstance(error, ValueError)
            )
            deferred = retryable and upstream is not None and upstream.defer_attempt
            operation = getattr(upstream or error, "monday_operation", "webhook_intake")
            retried = 0
            for event in events:
                if not deferred:
                    event.attempt_count += 1
                retry = retryable and (deferred or event.attempt_count < MAX_INTAKE_ATTEMPTS)
                retried += bool(retry)
                delay = retry_delay_seconds(
                    max(1, event.attempt_count),
                    base_seconds=settings.worker_retry_base_seconds,
                    max_seconds=settings.worker_retry_max_seconds,
                )
                if upstream is not None and upstream.retry_at is not None:
                    delay = max(delay, (upstream.retry_at - processed_at).total_seconds())
                event.status = WebhookEventStatus.FAILED.value
                event.processing_started_at = None
                event.processed_at = processed_at
                event.next_retry_at = processed_at + timedelta(seconds=delay) if retry else None
                event.error = json.dumps(safe_error_details(error), sort_keys=True)
            if retried:
                record_usage(session, "webhook_intake", "retries_scheduled", retried)
                if operation != "webhook_intake":
                    record_usage(session, operation, "retries_scheduled")
            if retried < len(events):
                record_usage(session, "webhook_intake", "failures", len(events) - retried)
        return True


def retry_failed_webhook(session: Session, event_id: uuid.UUID, *, now: datetime | None = None) -> WebhookEvent:
    event = session.get(WebhookEvent, event_id, with_for_update=True)
    if event is None or event.status != WebhookEventStatus.FAILED.value:
        raise ValueError("only a failed webhook event can be retried")
    event.status = WebhookEventStatus.RECEIVED.value
    event.attempt_count = 0
    event.next_retry_at = now or utc_now()
    event.error = None
    return event
