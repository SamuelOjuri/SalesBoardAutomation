from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import cast
from unittest.mock import Mock

import pytest
import requests

from app.config import BOARD_CONTRACT
from app.monday_client import MondayClient
from app.monday_errors import (
    MondayAPIError, MondayCooldownError, MondayRateLimitError, MondayTransientError,
    response_error, retry_after_seconds,
)
from app.services.worker import safe_error_details
from app.worker import _error_is_retryable


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def graphql_error(code, *, delay=None, status=None):
    extensions = {"code": code}
    if delay is not None:
        extensions["error_data"] = {"retry_in_seconds": delay}
    if status is not None:
        extensions["status_code"] = status
    return {"message": "PRIVATE-INPUT-DO-NOT-LOG", "extensions": extensions}


@pytest.mark.parametrize("status,code,expected", [
    (200, "UserUnauthorizedException", MondayAPIError),
    (200, "missingRequiredPermissions", MondayAPIError),
    (200, "ColumnValueException", MondayAPIError),
    (200, "GRAPHQL_VALIDATION_FAILED", MondayAPIError),
    (200, "UNKNOWN_NEW_ERROR", MondayAPIError),
    (401, None, MondayAPIError),
    (403, None, MondayAPIError),
    (400, None, MondayAPIError),
    (429, "REQUEST_MAX_COMPLEXITY_EXCEEDED", MondayAPIError),
    (200, "maxComplexityExceeded", MondayAPIError),
    (200, "COMPLEXITY_BUDGET_EXHAUSTED", MondayRateLimitError),
    (200, "DAILY_LIMIT_EXCEEDED", MondayRateLimitError),
    (429, "maxConcurrencyExceeded", MondayRateLimitError),
    (429, "IP_RATE_LIMIT_EXCEEDED", MondayRateLimitError),
    (429, None, MondayRateLimitError),
    (200, "API_TEMPORARILY_BLOCKED", MondayTransientError),
    (200, "INTERNAL_SERVER_ERROR", MondayTransientError),
    (409, "IDEMPOTENCY_CONFLICT", MondayTransientError),
    (423, None, MondayTransientError),
    (503, None, MondayTransientError),
])
def test_classification_preserves_codes_without_logging_payload(status, code, expected):
    payload = {"errors": [graphql_error(code)]} if code else {}
    payload["extensions"] = {"request_id": "request-123"}
    error = response_error(status, payload, {}, now=NOW)
    assert type(error) is expected
    assert error.code == (code or f"http_{status}")
    assert error.status_code == status
    assert error.request_id == "request-123"
    assert _error_is_retryable(error) == (expected is not MondayAPIError)
    assert "PRIVATE" not in str(error)
    assert "PRIVATE" not in str(safe_error_details(error))
    if code:
        assert safe_error_details(error)["errorCode"] == code


def test_permanent_error_cannot_be_hidden_by_limit_or_partial_data():
    error = response_error(200, {
        "data": {"items": [{"id": "42"}]},
        "errors": [graphql_error("DAILY_LIMIT_EXCEEDED"),
                   graphql_error("InvalidColumnIdException")],
    }, {"Retry-After": "120"}, now=NOW)
    assert type(error) is MondayAPIError
    assert error.codes == ("DAILY_LIMIT_EXCEEDED", "InvalidColumnIdException")
    assert error.retry_at is None


@pytest.mark.parametrize("http_status,expected", [
    (429, MondayRateLimitError), (503, MondayTransientError),
])
def test_transport_failure_status_is_not_masked_by_graphql_status(http_status, expected):
    error = response_error(http_status, {
        "errors": [graphql_error("UNKNOWN_ERROR", status=200)],
    }, {}, now=NOW)
    assert type(error) is expected


@pytest.mark.parametrize("headers,payload,expected", [
    ({"Retry-After": "120"}, {"retry_in_seconds": 180}, 180),
    ({"retry-after": "180"}, {"errors": [graphql_error("limit", delay=120)]}, 180),
    ({"Retry-After": format_datetime(NOW + timedelta(seconds=90), usegmt=True)}, {}, 90),
    ({"Retry-After": "garbage"}, {"extensions": {"retry_in_seconds": "45.5"}}, 45.5),
    ({"Retry-After": "-2"}, {"retry_in_seconds": -1}, None),
    ({"Retry-After": "inf"}, {"retry_in_seconds": "nan"}, None),
    ({"Retry-After": "1e300"}, {"retry_in_seconds": True}, None),
    ({}, {"data": {"retry_in_seconds": 9999}}, None),
])
def test_delay_parsing_honors_longest_valid_hint(headers, payload, expected):
    assert retry_after_seconds(headers, payload, now=NOW) == expected


def test_daily_exhaustion_waits_at_least_until_midnight_utc():
    for delay in (None, 60, 100000):
        error = response_error(200, {
            "errors": [graphql_error("DAILY_LIMIT_EXCEEDED", delay=delay)],
        }, {}, now=NOW)
        assert error.retry_at == max(
            NOW + timedelta(seconds=delay or 0),
            NOW.replace(day=23, hour=0),
        )


def test_daily_reset_uses_utc_even_with_a_local_clock():
    local_now = datetime(2026, 10, 22, 23, 30, tzinfo=timezone(timedelta(hours=-4)))
    error = response_error(200, {"errors": [graphql_error("DAILY_LIMIT_EXCEEDED")]}, {}, now=local_now)
    assert error.retry_at == datetime(2026, 10, 24, tzinfo=timezone.utc)


def session_with_response(status, payload, headers=None):
    session = Mock(spec=requests.Session)
    session.headers = {}
    response = Mock(spec=requests.Response)
    response.status_code = status
    response.headers = headers or {}
    if isinstance(payload, Exception):
        response.json.side_effect = payload
    else:
        response.json.return_value = payload
    session.post.return_value = response
    return session


@pytest.mark.parametrize("status,payload,expected", [
    (429, {"errors": [graphql_error("COMPLEXITY_BUDGET_EXHAUSTED", delay=120)]}, MondayRateLimitError),
    (200, {"errors": [graphql_error("DAILY_LIMIT_EXCEEDED")]}, MondayRateLimitError),
    (503, ValueError("non-json"), MondayTransientError),
    (401, ValueError("non-json"), MondayAPIError),
    (200, {"errors": [graphql_error("InvalidArgumentException")]}, MondayAPIError),
    (200, {"error_code": "DAILY_LIMIT_EXCEEDED", "retry_in_seconds": 120}, MondayRateLimitError),
])
def test_graphql_uses_one_http_attempt_and_blocks_subsequent_calls(status, payload, expected):
    session = session_with_response(status, payload)
    client = MondayClient(access_token="token", api_version="2026-07",
                          max_attempts=3, session=cast(requests.Session, session))
    with pytest.raises(expected) as captured:
        client.load_sales_item_intake("42")
    assert session.post.call_count == 1
    if captured.value.retry_at is not None:
        with pytest.raises(MondayCooldownError):
            client.load_sales_item_intake("42")
        assert session.post.call_count == 1


def test_transport_failure_has_one_attempt_and_remains_retryable():
    session = session_with_response(200, {})
    session.post.side_effect = requests.Timeout("private URL")
    client = MondayClient(access_token="token", api_version="2026-07", session=session)
    with pytest.raises(requests.Timeout):
        client.load_sales_item_intake("42")
    assert session.post.call_count == 1


def test_partial_data_is_never_accepted_as_success():
    session = session_with_response(200, {
        "data": {"items": [{"id": "42"}]},
        "errors": [graphql_error("UserUnauthorizedException")],
    })
    client = MondayClient(access_token="token", api_version="2026-07", session=session)
    with pytest.raises(MondayAPIError):
        client.load_sales_item_intake("42")


def test_wrapped_permanent_error_keeps_its_classification():
    upstream = response_error(200, {"errors": [graphql_error("UserUnauthorizedException")]}, {}, now=NOW)
    wrapper = RuntimeError("wrapper")
    wrapper.__cause__ = upstream
    assert not _error_is_retryable(wrapper)
    assert safe_error_details(wrapper)["errorCode"] == "UserUnauthorizedException"


def test_mutations_are_never_automatically_replayed():
    session = session_with_response(503, {})
    client = MondayClient(access_token="token", api_version="2026-07", session=session)
    with pytest.raises(MondayTransientError):
        client.change_sales_item_column_values(
            BOARD_CONTRACT.sales_board_id, "42", {BOARD_CONTRACT.postcode_column_id: {"ids": [115]}}
        )
    assert session.post.call_count == 1
