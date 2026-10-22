import json

import pytest
import requests

from app.database import Base, create_database_engine, create_session_factory
from app.monday_client import MondayClient
from app.monday_errors import MondayCooldownError, MondayRateLimitError
from app.services.operations import collect_processing_metrics
from app.services.usage import UsageRecorder


class Transport:
    headers: dict[str, str] = {}

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        status, payload, headers = value
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = json.dumps(payload).encode()
        return response


def test_usage_aggregates_workers_and_distinguishes_requests_from_cooldown_skips(tmp_path, caplog):
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'usage.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    transport = Transport([
        (200, {"data": {"items": [{"id": "42"}]}}, {}),
        (429, {"errors": [{"message": "PRIVATE-TOKEN", "extensions": {"code": "DAILY_LIMIT_EXCEEDED"}}]}, {"Retry-After": "3600"}),
    ])
    first = MondayClient(access_token="PRIVATE-TOKEN", api_version="2026-07", session=transport, usage=UsageRecorder(factory))
    second = MondayClient(access_token="PRIVATE-TOKEN", api_version="2026-07", session=transport, usage=UsageRecorder(factory))
    first.load_sales_item_intake("42")
    with pytest.raises(MondayRateLimitError):
        second.load_sales_item_intake("42")
    with pytest.raises(MondayCooldownError):
        second.load_sales_item_intake("42")
    with factory() as session:
        metrics = collect_processing_metrics(session, lease_timeout_seconds=300).as_dict()
        assert metrics["mondayUsage"]["SalesItemIntake"] == {
            "requests": 2, "successes": 1, "failures": 1,
            "retryable_failures": 1, "cooldown_skips": 1,
        }
    assert transport.calls == 2
    assert "PRIVATE-TOKEN" not in caplog.text
    engine.dispose()


def test_metrics_failure_does_not_repeat_or_fail_a_successful_request():
    def unavailable():
        raise RuntimeError("metrics database unavailable")

    transport = Transport([(200, {"data": {"items": [{"id": "42"}]}}, {})])
    client = MondayClient(access_token="token", api_version="2026-07", session=transport, usage=UsageRecorder(unavailable))
    assert client.load_sales_item_intake("42")["id"] == "42"
    assert transport.calls == 1
