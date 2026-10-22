from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import uuid

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import Base, create_database_engine, create_session_factory
from app.models import MondayCooldown
from app.monday_client import MondayClient
from app.monday_errors import MondayCooldownError, MondayRateLimitError
from app.services.monday_cooldown import SharedMondayCooldown, monday_cooldown_scope
from test_monday_retries import session_with_response


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def cooldown_database(tmp_path):
    url = os.environ.get("MONDAY_COOLDOWN_TEST_DATABASE_URL")
    admin_engine = None
    schema = None
    if url:
        # Only an explicit disposable test database; never application DATABASE_URL.
        admin_engine = create_database_engine(url)
        assert admin_engine.dialect.name == "postgresql"
        schema = f"test_monday_cooldown_{uuid.uuid4().hex}"
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
    else:
        url = f"sqlite+pysqlite:///{(tmp_path / 'cooldown.sqlite3').as_posix()}"
    engines = []

    def connect():
        engine = create_database_engine(url)
        if schema:
            engine = engine.execution_options(schema_translate_map={None: schema})
        if not engines:
            Base.metadata.create_all(engine)
        engines.append(engine)
        factory = create_session_factory(engine)
        return SharedMondayCooldown(session_factory=factory, source_scope="source"), factory

    yield connect
    for engine in engines:
        engine.dispose()
    if admin_engine is not None:
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def test_cooldown_survives_restarts_and_an_unrelated_transaction_rollback(cooldown_database):
    connect = cooldown_database
    first, factory = connect()
    deadline = NOW + timedelta(days=1)
    with factory() as unrelated:
        first.record(MondayRateLimitError("safe", code="DAILY_LIMIT_EXCEEDED", retry_at=deadline))
        unrelated.rollback()
    second, _ = connect()
    blocked = second.active_error(now=NOW)
    assert isinstance(blocked, MondayCooldownError)
    assert blocked.code == "DAILY_LIMIT_EXCEEDED"
    assert blocked.retry_at == deadline
    assert second.active_error(now=deadline) is None


def test_concurrent_updates_keep_the_longest_deadline(cooldown_database):
    first, _ = cooldown_database()
    second, _ = cooldown_database()
    def record(index):
        (first if index % 2 else second).record(MondayRateLimitError(
            "safe", code=f"limit_{index}", retry_at=NOW + timedelta(seconds=60 + index)
        ))
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record, range(12)))
    error = second.active_error(now=NOW)
    assert error.retry_at == NOW + timedelta(seconds=71)
    assert error.code == "limit_11"


def test_other_sources_do_not_share_cooldowns_or_persist_secrets(cooldown_database):
    first, factory = cooldown_database()
    first.record(MondayRateLimitError("safe", code="limit", retry_at=NOW + timedelta(minutes=1)))
    other = SharedMondayCooldown(session_factory=factory, source_scope="other")
    assert other.active_error(now=NOW) is None
    scope = monday_cooldown_scope(api_url="https://api.monday.com/v2", access_token="SECRET")
    assert len(scope) == 64 and "SECRET" not in scope
    assert scope == monday_cooldown_scope(api_url="https://api.monday.com/v2/", access_token="SECRET")
    assert scope != monday_cooldown_scope(api_url="https://api.monday.com/v2", access_token="OTHER")


def test_response_from_one_client_prevents_http_calls_from_other_clients(cooldown_database):
    first, factory = cooldown_database()
    second, _ = cooldown_database()
    first_http = session_with_response(429, {"retry_in_seconds": 86400})
    second_http = session_with_response(200, {"data": {"items": [{"id": "42"}]}})
    first_client = MondayClient(access_token="token", api_version="2026-07", session=first_http, cooldown=first)
    second_client = MondayClient(access_token="token", api_version="2026-10", session=second_http, cooldown=second)
    with pytest.raises(MondayRateLimitError):
        first_client.load_sales_item_intake("42")
    with pytest.raises(MondayCooldownError):
        second_client.load_sales_item_intake("42")
    assert first_http.post.call_count == 1
    second_http.post.assert_not_called()
    # Expiry permits a fresh authoritative read.
    with factory.begin() as session:
        session.get(MondayCooldown, "source").retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert second_client.load_sales_item_intake("42") == {"id": "42"}
    assert second_http.post.call_count == 1
