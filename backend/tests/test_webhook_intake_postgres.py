"""Opt-in PostgreSQL locking tests using isolated disposable schemas."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.models import ProcessingJob, WebhookEvent
from app.routes.monday_webhooks import _claim_event, normalize_webhook_payload
from app.services.webhook_intake import process_next_webhook_intake
from test_monday_webhooks import FakeMondayClient, intake_item, runtime_settings, webhook_payload
from test_reference_cache import cache_database


@pytest.fixture
def pg_factory(cache_database):
    connect, _ = cache_database
    factory = connect()
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Set REFERENCE_CACHE_TEST_DATABASE_URL to a disposable PostgreSQL database")
    return factory


def persist(factory, trigger):
    payload = webhook_payload(triggerUuid=trigger)
    with factory.begin() as session:
        return _claim_event(session, payload, normalize_webhook_payload(payload))


def test_concurrent_workers_and_arrivals_during_snapshot(pg_factory):
    for i in range(8):
        persist(pg_factory, f"burst-{i}")
    entered, release = Event(), Event()

    class BlockingClient(FakeMondayClient):
        def load_sales_item_intake(self, item_id):
            entered.set()
            assert release.wait(10)
            return super().load_sales_item_intake(item_id)

    client = BlockingClient(intake_item())
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(process_next_webhook_intake, pg_factory, client, settings=runtime_settings())
        try:
            assert entered.wait(5)
            second = pool.submit(process_next_webhook_intake, pg_factory, client, settings=runtime_settings())
            assert second.result(timeout=5) is False
            # Accepted inserts and exact duplicate deliveries must not wait for
            # Monday or become part of a snapshot that predates their receipt.
            _, created = persist(pg_factory, "arrived-during-read")
            assert created
            _, created = persist(pg_factory, "burst-0")
            assert not created
        finally:
            release.set()
        assert first.result(timeout=5)
    assert client.requested_item_ids == ["42"]
    with pg_factory() as session:
        assert session.query(WebhookEvent).filter_by(status="processed").count() == 8
        assert session.query(WebhookEvent).filter_by(status="received").count() == 1
    assert process_next_webhook_intake(pg_factory, client, settings=runtime_settings())
    assert client.requested_item_ids == ["42", "42"]


def test_worker_crash_rolls_back_and_restart_reclaims_every_event(pg_factory, monkeypatch):
    from app.services import webhook_intake
    for i in range(3):
        persist(pg_factory, str(i))
    client = FakeMondayClient(intake_item())
    queue = webhook_intake.queue_sales_item_snapshot

    def crash_after_queue(*args, **kwargs):
        queue(*args, **kwargs)
        raise KeyboardInterrupt("simulated worker exit")

    monkeypatch.setattr(webhook_intake, "queue_sales_item_snapshot", crash_after_queue)
    with pytest.raises(KeyboardInterrupt):
        process_next_webhook_intake(pg_factory, client, settings=runtime_settings())
    with pg_factory() as session:
        assert session.query(ProcessingJob).count() == 0
        assert all(e.status == "received" for e in session.query(WebhookEvent))
    monkeypatch.setattr(webhook_intake, "queue_sales_item_snapshot", queue)
    assert process_next_webhook_intake(pg_factory, client, settings=runtime_settings())
    with pg_factory() as session:
        assert session.query(WebhookEvent).filter_by(status="processed").count() == 3
