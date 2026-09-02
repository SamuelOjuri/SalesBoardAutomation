import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from app.config import (
    BOARD_CONTRACT,
    DEFAULT_EXCLUDED_SALES_GROUP_IDS,
    Settings,
    build_processing_pipeline_version,
)
from app.database import Base, create_database_engine, create_session_factory
from app.main import create_app
from app.models import ProcessingJob, WebhookEvent, WebhookEventStatus
from app.monday_client import MondayClient


class FakeMondayClient:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.requested_item_ids: list[str] = []

    def load_sales_item_intake(self, item_id: str) -> dict[str, Any]:
        self.requested_item_ids.append(item_id)
        return self.item


def runtime_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://user:password@localhost/sales",
        "monday_ingestion_access_token": "token",
        "monday_webhook_shared_secret": "shared-secret",
        "gemini_api_key": "gemini-key",
        "gemini_model": "gemini-test-model",
        "processing_pipeline_version": build_processing_pipeline_version(
            "gemini-test-model"
        ),
    }
    values.update(updates)
    return Settings(_env_file=None, **values)


def valid_board() -> dict[str, Any]:
    return {
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
                "settings": {
                    "labels": [
                        {"id": label.id, "label": label.name}
                        for label in BOARD_CONTRACT.required_postcode_labels
                    ]
                },
            },
        ],
    }


def intake_item(
    *,
    state: str = "active",
    filename: str = "request.eml",
    group_id: str = "topics",
) -> dict[str, Any]:
    return {
        "id": "42",
        "state": state,
        "board": {"id": str(BOARD_CONTRACT.sales_board_id)},
        "group": {"id": group_id, "title": "Test Group"},
        "assets": [
            {
                "id": "7",
                "name": filename,
                "file_size": 12,
                "created_at": "2026-08-19T09:30:00Z",
                "url": "https://files.monday.com/request.eml",
                "public_url": "https://files.monday.com/public/request.eml",
            }
        ],
        "column_values": [
            {
                "id": BOARD_CONTRACT.email_file_column_id,
                "type": "file",
                "value": json.dumps({"files": [{"assetId": 7}]}),
            }
        ],
    }


def webhook_payload(**updates: object) -> dict[str, Any]:
    event: dict[str, object] = {
        "triggerUuid": "trigger-1",
        "boardId": BOARD_CONTRACT.sales_board_id,
        "pulseId": 42,
        "columnId": BOARD_CONTRACT.email_file_column_id,
        "type": "change_column_value",
    }
    event.update(updates)
    return {"event": event}


@pytest.fixture
def webhook_application(tmp_path: Path) -> tuple[FastAPI, Engine, FakeMondayClient]:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'webhooks.db').as_posix()}"
    )
    Base.metadata.create_all(engine)
    monday_client = FakeMondayClient(intake_item())
    application = create_app(
        settings=runtime_settings(),
        schema_loader=lambda _: valid_board(),
        engine=engine,
        monday_client=cast(MondayClient, monday_client),
    )
    try:
        yield application, engine, monday_client
    finally:
        engine.dispose()


def post_requests(
    application: FastAPI,
    requests: list[tuple[dict[str, Any], dict[str, str]]],
    *,
    base_url: str = "https://testserver",
) -> list[httpx.Response]:
    async def send() -> list[httpx.Response]:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport, base_url=base_url
            ) as client:
                return [
                    await client.post(
                        "/api/monday/webhooks", json=payload, headers=headers
                    )
                    for payload, headers in requests
                ]

    return asyncio.run(send())


def shared_secret_headers() -> dict[str, str]:
    return {"X-Monday-Webhook-Secret": "shared-secret"}


def test_challenge_is_echoed_without_processing(webhook_application: tuple[Any, ...]) -> None:
    application, _, monday_client = webhook_application

    response = post_requests(application, [({"challenge": "abc123"}, {})])[0]

    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123"}
    assert monday_client.requested_item_ids == []


def test_repeated_delivery_creates_one_event_and_one_active_job(
    webhook_application: tuple[Any, ...]
) -> None:
    application, engine, monday_client = webhook_application
    request = (webhook_payload(), shared_secret_headers())

    first, duplicate = post_requests(application, [request, request])

    assert first.status_code == 200
    assert first.json()["status"] == "queued"
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert monday_client.requested_item_ids == ["42"]
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        assert session.query(WebhookEvent).count() == 1
        assert session.query(ProcessingJob).count() == 1


def test_stale_processing_event_is_reclaimed(
    webhook_application: tuple[Any, ...]
) -> None:
    application, engine, monday_client = webhook_application
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        session.add(
            WebhookEvent(
                idempotency_key="trigger:trigger-1",
                trigger_uuid="trigger-1",
                board_id=str(BOARD_CONTRACT.sales_board_id),
                item_id="42",
                column_id=BOARD_CONTRACT.email_file_column_id,
                event_type="change_column_value",
                payload_json=webhook_payload(),
                authenticated=True,
                status=WebhookEventStatus.PROCESSING.value,
                processing_started_at=datetime.now(timezone.utc)
                - timedelta(minutes=6),
                attempt_count=1,
            )
        )
        session.commit()

    response = post_requests(
        application, [(webhook_payload(), shared_secret_headers())]
    )[0]

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert monday_client.requested_item_ids == ["42"]
    with session_factory() as session:
        event = session.query(WebhookEvent).one()
        assert event.status == WebhookEventStatus.PROCESSED.value
        assert event.attempt_count == 2


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (intake_item(state="archived"), "item_not_active"),
        (intake_item(filename="notes.pdf"), "no_supported_email"),
    ],
)
def test_ineligible_authoritative_snapshot_does_not_enqueue(
    webhook_application: tuple[Any, ...], item: dict[str, Any], reason: str
) -> None:
    application, engine, monday_client = webhook_application
    monday_client.item = item

    response = post_requests(
        application, [(webhook_payload(), shared_secret_headers())]
    )[0]

    assert response.status_code == 200
    assert response.json()["reason"] == reason
    with create_session_factory(engine)() as session:
        assert session.query(ProcessingJob).count() == 0


def test_excluded_authoritative_group_does_not_enqueue(
    webhook_application: tuple[Any, ...]
) -> None:
    application, engine, monday_client = webhook_application
    monday_client.item = intake_item(
        group_id=DEFAULT_EXCLUDED_SALES_GROUP_IDS[0]
    )
    monday_client.item["column_values"] = []

    response = post_requests(
        application, [(webhook_payload(), shared_secret_headers())]
    )[0]

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "group_excluded"
    with create_session_factory(engine)() as session:
        event = session.query(WebhookEvent).one()
        assert event.authenticated is True
        assert event.status == WebhookEventStatus.PROCESSED.value
        assert session.query(ProcessingJob).count() == 0


def test_wrong_board_or_column_is_persisted_without_refetching(
    webhook_application: tuple[Any, ...]
) -> None:
    application, engine, monday_client = webhook_application

    response = post_requests(
        application,
        [
            (
                webhook_payload(
                    triggerUuid="wrong-column", columnId="unmanaged_column"
                ),
                shared_secret_headers(),
            )
        ],
    )[0]

    assert response.status_code == 200
    assert response.json()["reason"] == "column_not_managed"
    assert monday_client.requested_item_ids == []
    with create_session_factory(engine)() as session:
        assert session.query(WebhookEvent).count() == 1
        assert session.query(ProcessingJob).count() == 0


def test_webhook_rejects_insecure_or_unauthenticated_requests(
    webhook_application: tuple[Any, ...]
) -> None:
    application, _, _ = webhook_application

    insecure = post_requests(
        application,
        [(webhook_payload(), shared_secret_headers())],
        base_url="http://testserver",
    )[0]
    unauthenticated = post_requests(application, [(webhook_payload(), {})])[0]

    assert insecure.status_code == 400
    assert unauthenticated.status_code == 401


@pytest.mark.parametrize(
    "authorization_style",
    [
        pytest.param("raw", id="raw-jwt"),
        pytest.param("bearer", id="bearer-jwt"),
    ],
)
def test_webhook_accepts_a_valid_expiring_hs256_token(
    tmp_path: Path, authorization_style: str
) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'jwt-webhooks.db').as_posix()}"
    )
    Base.metadata.create_all(engine)
    monday_client = FakeMondayClient(intake_item())
    signing_secret = "signing-secret-with-at-least-32-bytes"
    application = create_app(
        settings=runtime_settings(
            monday_webhook_shared_secret=None,
            monday_signing_secret=signing_secret,
        ),
        schema_loader=lambda _: valid_board(),
        engine=engine,
        monday_client=cast(MondayClient, monday_client),
    )
    token = jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        signing_secret,
        algorithm="HS256",
    )
    authorization = token if authorization_style == "raw" else f"Bearer {token}"
    try:
        response = post_requests(
            application,
            [(webhook_payload(), {"Authorization": authorization})],
        )[0]
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
    finally:
        engine.dispose()
