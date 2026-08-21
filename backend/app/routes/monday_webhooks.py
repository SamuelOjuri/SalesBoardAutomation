"""Authenticated, idempotent Monday webhook intake."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

import jwt
from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import BoardContract
from app.database import session_scope
from app.models import WebhookEvent, WebhookEventStatus
from app.monday_client import MondayAPIError, MondayTransientError
from app.services.intake import (
    IntakeContractError,
    is_excluded_sales_group,
    parse_sales_item_snapshot,
    queue_sales_item_snapshot,
)


router = APIRouter(prefix="/api/monday/webhooks", tags=["monday"])
WEBHOOK_PROCESSING_LEASE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class NormalizedWebhookEvent:
    idempotency_key: str
    monday_event_id: str | None
    subscription_id: str | None
    trigger_uuid: str | None
    board_id: str | None
    item_id: str | None
    group_id: str | None
    event_type: str | None
    column_id: str | None


def normalize_webhook_payload(payload: Mapping[str, Any]) -> NormalizedWebhookEvent:
    nested_event = payload.get("event")
    event = nested_event if isinstance(nested_event, Mapping) else payload
    board_id = _first_string(
        event, ("boardId", "board_id", "pulseBoardId", "pulse_board_id")
    )
    item_id = _first_string(event, ("itemId", "item_id", "pulseId", "pulse_id"))
    trigger_uuid = _first_string(event, ("triggerUuid", "trigger_uuid"))
    monday_event_id = _first_string(event, ("eventId", "event_id", "id"))
    subscription_id = _first_string(
        event,
        (
            "subscriptionId",
            "subscription_id",
            "webhookId",
            "webhook_id",
            "appWebhookId",
            "app_webhook_id",
        ),
    )
    event_type = _first_string(event, ("type", "eventType", "event_type", "event"))
    column_id = _first_string(event, ("columnId", "column_id"))
    group_id = _first_string(
        event,
        (
            "groupId",
            "group_id",
            "destGroupId",
            "dest_group_id",
            "destinationGroupId",
            "destination_group_id",
            "newGroupId",
            "new_group_id",
        ),
    )
    if trigger_uuid:
        idempotency_key = f"trigger:{trigger_uuid}"
    elif monday_event_id and subscription_id:
        idempotency_key = f"event:{subscription_id}:{monday_event_id}"
    elif board_id and item_id and event_type:
        idempotency_key = (
            f"payload:{board_id}:{item_id}:{event_type}:{_payload_hash(payload)}"
        )
    else:
        idempotency_key = f"payload:{_payload_hash(payload)}"
    return NormalizedWebhookEvent(
        idempotency_key=idempotency_key,
        monday_event_id=monday_event_id,
        subscription_id=subscription_id,
        trigger_uuid=trigger_uuid,
        board_id=board_id,
        item_id=item_id,
        group_id=group_id,
        event_type=event_type,
        column_id=column_id,
    )


@router.post("")
@router.post("/", include_in_schema=False)
def monday_webhook(
    payload: dict[str, Any],
    request: Request,
    authorization: str | None = Header(default=None),
    shared_secret_header: str | None = Header(
        default=None, alias="X-Monday-Webhook-Secret"
    ),
    shared_secret_query: str | None = Query(default=None, alias="token"),
) -> dict[str, Any]:
    challenge = payload.get("challenge")
    if challenge is not None:
        return {"challenge": challenge}

    _require_https(request)
    _verify_authorization(
        request,
        authorization=authorization,
        shared_secret=shared_secret_header or shared_secret_query,
    )
    normalized = normalize_webhook_payload(payload)
    with session_scope(request.app.state.session_factory) as session:
        event, claimed = _claim_event(session, payload, normalized)
    if not claimed:
        return {
            "status": (
                "in_progress"
                if event.status == WebhookEventStatus.PROCESSING.value
                else "duplicate"
            ),
            "eventId": str(event.id),
            "idempotencyKey": event.idempotency_key,
        }

    settings = request.app.state.settings
    ignored_reason = _ignored_reason(normalized, settings.board_contract)
    if ignored_reason is not None:
        _finish_event(request, event.id)
        return _event_response(event, status="ignored", reason=ignored_reason)

    try:
        raw_item = request.app.state.monday_client.load_sales_item_intake(
            normalized.item_id
        )
        snapshot = parse_sales_item_snapshot(
            raw_item, contract=settings.board_contract
        )
        if snapshot.board_id != str(settings.sales_board_id):
            _finish_event(request, event.id)
            return _event_response(event, status="ignored", reason="board_not_managed")
        if snapshot.item_id != normalized.item_id:
            raise IntakeContractError("Monday returned the wrong Sales item")
        if is_excluded_sales_group(
            snapshot.group_id,
            settings.processing_excluded_group_ids,
        ):
            _finish_event(request, event.id)
            return _event_response(event, status="ignored", reason="group_excluded")
        if not snapshot.active:
            _finish_event(request, event.id)
            return _event_response(event, status="ignored", reason="item_not_active")
        if not snapshot.email_assets:
            _finish_event(request, event.id)
            return _event_response(event, status="ignored", reason="no_supported_email")

        with session_scope(request.app.state.session_factory) as session:
            result = queue_sales_item_snapshot(
                session,
                snapshot,
                webhook_event_id=event.id,
                pipeline_version=settings.processing_pipeline_version,
                excluded_group_ids=settings.processing_excluded_group_ids,
            )
            event_record = session.get(WebhookEvent, event.id)
            if event_record is None:
                raise RuntimeError("claimed webhook event disappeared")
            event_record.status = WebhookEventStatus.PROCESSED.value
            event_record.processing_started_at = None
            event_record.processed_at = datetime.now(timezone.utc)
            event_record.error = None
        return {
            **_event_response(event, status=result.outcome),
            "jobId": str(result.job.id),
            "inputRevision": result.item.latest_input_revision,
        }
    except (MondayAPIError, MondayTransientError, IntakeContractError) as error:
        _fail_event(request, event.id, error)
        raise HTTPException(
            status_code=503,
            detail="Webhook intake temporarily unavailable",
            headers={"Retry-After": "1"},
        ) from error
    except Exception as error:
        _fail_event(request, event.id, error)
        raise HTTPException(
            status_code=503,
            detail="Webhook intake temporarily unavailable",
            headers={"Retry-After": "1"},
        ) from error


def _verify_authorization(
    request: Request,
    *,
    authorization: str | None,
    shared_secret: str | None,
) -> None:
    settings = request.app.state.settings
    configured_shared_secret = settings.monday_webhook_shared_secret
    if configured_shared_secret is not None and shared_secret is not None:
        if hmac.compare_digest(
            shared_secret,
            configured_shared_secret.get_secret_value(),
        ):
            return

    signing_secret = settings.monday_signing_secret
    if signing_secret is None or authorization is None:
        raise HTTPException(status_code=401, detail="Invalid Monday webhook authorization")

    token = authorization.strip()
    scheme, separator, credentials = token.partition(" ")
    if separator:
        if scheme.casefold() != "bearer" or not credentials.strip():
            raise HTTPException(
                status_code=401, detail="Invalid Monday webhook authorization"
            )
        token = credentials.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid Monday webhook authorization")
    try:
        jwt.decode(
            token,
            signing_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp"], "verify_aud": False},
        )
    except jwt.PyJWTError as error:
        raise HTTPException(
            status_code=401, detail="Invalid Monday webhook authorization"
        ) from error


def _require_https(request: Request) -> None:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0]
    if request.url.scheme != "https" and forwarded_proto.strip().casefold() != "https":
        raise HTTPException(status_code=400, detail="Monday webhooks must use HTTPS")


def _claim_event(
    session: Session,
    payload: dict[str, Any],
    normalized: NormalizedWebhookEvent,
) -> tuple[WebhookEvent, bool]:
    query = session.query(WebhookEvent).filter_by(
        idempotency_key=normalized.idempotency_key
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    existing = query.one_or_none()
    if existing is not None:
        now = datetime.now(timezone.utc)
        processing_started_at = existing.processing_started_at
        if processing_started_at is not None and processing_started_at.tzinfo is None:
            processing_started_at = processing_started_at.replace(tzinfo=timezone.utc)
        stale_processing = (
            existing.status == WebhookEventStatus.PROCESSING.value
            and (
                processing_started_at is None
                or processing_started_at <= now - WEBHOOK_PROCESSING_LEASE
            )
        )
        if existing.status not in {
            WebhookEventStatus.RECEIVED.value,
            WebhookEventStatus.FAILED.value,
        } and not stale_processing:
            return existing, False
        existing.status = WebhookEventStatus.PROCESSING.value
        existing.processing_started_at = now
        existing.processed_at = None
        existing.error = None
        existing.attempt_count += 1
        existing.payload_json = payload
        return existing, True

    event = WebhookEvent(
        id=uuid.uuid4(),
        idempotency_key=normalized.idempotency_key,
        monday_event_id=normalized.monday_event_id,
        subscription_id=normalized.subscription_id,
        trigger_uuid=normalized.trigger_uuid,
        board_id=normalized.board_id,
        item_id=normalized.item_id,
        group_id=normalized.group_id,
        event_type=normalized.event_type,
        column_id=normalized.column_id,
        payload_json=payload,
        authenticated=True,
        status=WebhookEventStatus.PROCESSING.value,
        processing_started_at=datetime.now(timezone.utc),
        attempt_count=1,
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush([event])
        return event, True
    except IntegrityError:
        existing = session.query(WebhookEvent).filter_by(
            idempotency_key=normalized.idempotency_key
        ).one()
        return existing, False


def _ignored_reason(
    normalized: NormalizedWebhookEvent, contract: BoardContract
) -> str | None:
    if normalized.board_id != str(contract.sales_board_id):
        return "board_not_managed"
    if normalized.column_id != contract.email_file_column_id:
        return "column_not_managed"
    if normalized.item_id is None:
        return "missing_item_id"
    return None


def _finish_event(request: Request, event_id: uuid.UUID) -> None:
    with session_scope(request.app.state.session_factory) as session:
        event = session.get(WebhookEvent, event_id)
        if event is None:
            raise RuntimeError("claimed webhook event disappeared")
        event.status = WebhookEventStatus.PROCESSED.value
        event.processing_started_at = None
        event.processed_at = datetime.now(timezone.utc)
        event.error = None


def _fail_event(request: Request, event_id: uuid.UUID, error: Exception) -> None:
    with session_scope(request.app.state.session_factory) as session:
        event = session.get(WebhookEvent, event_id)
        if event is None:
            return
        event.status = WebhookEventStatus.FAILED.value
        event.processing_started_at = None
        event.processed_at = datetime.now(timezone.utc)
        event.error = str(error)[:2000]


def _event_response(
    event: WebhookEvent, *, status: str, reason: str | None = None
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": status,
        "eventId": str(event.id),
        "idempotencyKey": event.idempotency_key,
    }
    if reason is not None:
        response["reason"] = reason
    return response


def _first_string(source: Mapping[str, Any], names: Iterable[str]) -> str | None:
    for name in names:
        value = source.get(name)
        if value is not None:
            normalized = str(value).strip()
            if normalized:
                return normalized
    return None


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
