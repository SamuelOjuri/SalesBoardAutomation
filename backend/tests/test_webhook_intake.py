from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sqlalchemy_event

from app.models import ProcessingAudit, ProcessingJob, WebhookEvent
from app.monday_errors import MondayAPIError, MondayRateLimitError, MondayTransientError
from app.services.webhook_intake import process_next_webhook_intake, retry_failed_webhook
from test_monday_webhooks import (
    post_requests, runtime_settings,
    shared_secret_headers, webhook_application, webhook_payload,
)
from app.database import create_session_factory

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def accept(application, count=1):
    responses = post_requests(application, [
        (webhook_payload(triggerUuid=f"burst-{i}"), shared_secret_headers())
        for i in range(count)
    ])
    assert all(response.json()["status"] == "accepted" for response in responses)


def test_burst_retains_every_payload_and_uses_one_snapshot(webhook_application):
    application, engine, client = webhook_application
    accept(application, 12)
    factory = create_session_factory(engine)
    assert client.requested_item_ids == []
    with factory() as session:
        assert session.query(WebhookEvent).count() == 12
        assert session.query(ProcessingJob).count() == 0
    assert process_next_webhook_intake(factory, client, settings=runtime_settings(), now=NOW)
    with factory() as session:
        assert session.query(ProcessingJob).count() == 1
        events = session.query(WebhookEvent).all()
        assert all(event.status == "processed" for event in events)
        assert len({event.payload_json["event"]["triggerUuid"] for event in events}) == 12
        assert {audit.webhook_event_id for audit in session.query(ProcessingAudit)} == {e.id for e in events}
    assert client.requested_item_ids == ["42"]
    assert not process_next_webhook_intake(factory, client, settings=runtime_settings(), now=NOW)


@pytest.mark.parametrize("failure,attempts,retry_seconds", [
    (MondayRateLimitError("limited", code="DAILY_LIMIT_EXCEEDED", retry_at=NOW + timedelta(hours=8)), 0, 28800),
    (MondayTransientError("temporary", retry_at=NOW + timedelta(minutes=30)), 1, 1800),
    (TimeoutError("PRIVATE-TOKEN"), 1, 30),
    (MondayAPIError("PRIVATE-TOKEN", code="missingRequiredPermissions"), 1, None),
])
def test_durable_failure_uses_quota_policy_and_safe_errors(webhook_application, failure, attempts, retry_seconds):
    application, engine, client = webhook_application
    accept(application, 2)
    factory = create_session_factory(engine)
    calls = []

    class FailingClient:
        def load_sales_item_intake(self, item_id):
            calls.append(item_id)
            raise failure

    assert process_next_webhook_intake(factory, FailingClient(), settings=runtime_settings(), now=NOW)
    with factory() as session:
        events = session.query(WebhookEvent).all()
        assert all(e.status == "failed" and e.attempt_count == attempts for e in events)
        assert all("PRIVATE-TOKEN" not in e.error for e in events)
        deadline = events[0].next_retry_at
        assert deadline == (NOW + timedelta(seconds=retry_seconds)).replace(tzinfo=None) if retry_seconds else deadline is None
        assert session.query(ProcessingJob).count() == 0
    assert not process_next_webhook_intake(factory, FailingClient(), settings=runtime_settings(), now=NOW + timedelta(seconds=1))
    assert calls == ["42"]
    if retry_seconds:
        # A different worker/process can finish the same durable records.
        assert process_next_webhook_intake(factory, client, settings=runtime_settings(), now=NOW + timedelta(seconds=retry_seconds))
        with factory() as session:
            assert all(e.status == "processed" for e in session.query(WebhookEvent))


def test_retry_budget_and_explicit_operator_recovery(webhook_application):
    application, engine, client = webhook_application
    accept(application)
    factory = create_session_factory(engine)

    class FailingClient:
        def load_sales_item_intake(self, item_id):
            raise TimeoutError()

    for seconds in (0, 30, 90):
        assert process_next_webhook_intake(factory, FailingClient(), settings=runtime_settings(), now=NOW + timedelta(seconds=seconds))
    assert not process_next_webhook_intake(factory, FailingClient(), settings=runtime_settings(), now=NOW + timedelta(days=1))
    with factory.begin() as session:
        event = session.query(WebhookEvent).one()
        assert event.attempt_count == 3 and event.next_retry_at is None
        retry_failed_webhook(session, event.id, now=NOW)
    assert process_next_webhook_intake(factory, client, settings=runtime_settings(), now=NOW)


def test_queue_failure_rolls_back_all_event_audits_and_retries(webhook_application, monkeypatch):
    from app.services import webhook_intake
    application, engine, client = webhook_application
    accept(application, 2)
    factory = create_session_factory(engine)
    queue = webhook_intake.queue_sales_item_snapshot
    calls = []

    def fail_second(*args, **kwargs):
        result = queue(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            raise RuntimeError("queue unavailable")
        return result

    monkeypatch.setattr(webhook_intake, "queue_sales_item_snapshot", fail_second)
    assert process_next_webhook_intake(factory, client, settings=runtime_settings(), now=NOW)
    with factory() as session:
        assert session.query(ProcessingJob).count() == 0
        assert session.query(ProcessingAudit).count() == 0
        assert all(e.status == "failed" for e in session.query(WebhookEvent))
    monkeypatch.setattr(webhook_intake, "queue_sales_item_snapshot", queue)
    assert process_next_webhook_intake(factory, client, settings=runtime_settings(), now=NOW + timedelta(seconds=30))
    with factory() as session:
        assert session.query(ProcessingAudit).count() == 2


def test_acknowledgement_requires_database_commit(webhook_application):
    application, engine, client = webhook_application
    from sqlalchemy.orm import Session

    def reject_commit(session):
        raise RuntimeError("database commit unavailable")

    sqlalchemy_event.listen(Session, "before_commit", reject_commit)
    try:
        with pytest.raises(RuntimeError, match="commit unavailable"):
            accept(application)
    finally:
        sqlalchemy_event.remove(Session, "before_commit", reject_commit)
    assert client.requested_item_ids == []


def test_worker_drains_intake_with_analysis_disabled(webhook_application, monkeypatch):
    from app.worker import WorkerRuntime, process_next_job
    application, engine, client = webhook_application
    accept(application, 3)
    factory = create_session_factory(engine)
    monkeypatch.setattr(client, "cooldown_error", lambda **kwargs: None, raising=False)
    runtime = WorkerRuntime(
        settings=runtime_settings(processing_mode="off"), engine=engine,
        session_factory=factory, monday_client=client, dependencies=None,
        worker_id="intake-worker",
    )
    assert process_next_job(runtime, now=NOW)
    with factory() as session:
        assert session.query(WebhookEvent).filter_by(status="processed").count() == 3
        assert session.query(ProcessingJob).one().status == "scheduled"
    assert client.requested_item_ids == ["42"]


def test_worker_leaves_durable_intake_untouched_during_cooldown(webhook_application, monkeypatch):
    from app.worker import WorkerRuntime, process_next_job
    application, engine, client = webhook_application
    accept(application, 3)
    factory = create_session_factory(engine)
    monkeypatch.setattr(client, "cooldown_error", lambda **kwargs: MondayRateLimitError("cooldown"), raising=False)
    runtime = WorkerRuntime(
        settings=runtime_settings(), engine=engine, session_factory=factory,
        monday_client=client, dependencies=None, worker_id="intake-worker",
    )
    assert not process_next_job(runtime, now=NOW)
    with factory() as session:
        assert session.query(WebhookEvent).filter_by(status="received").count() == 3
        assert all(e.attempt_count == 0 for e in session.query(WebhookEvent))
    assert client.requested_item_ids == []
