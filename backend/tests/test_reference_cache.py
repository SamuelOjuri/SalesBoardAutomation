from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Event
import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.schema import CreateSchema, DropSchema

from app.behavioral_contract import AccountResolution
from app.config import BOARD_CONTRACT
from app.database import Base, create_database_engine, create_session_factory
from app.models import ReferenceIndexSnapshot
from app.services.accounts import (
    AccountsContractError,
    AccountsIndexService,
    ContactsIndexService,
    match_account,
)
from app.services.reference_cache import SharedIndexCache, reference_cache_scope
from app.services.requester_identity import RequesterIdentity, email_address_sha256
from test_accounts import FakeAccountsClient, FakeContactsClient, _account, _contact_item


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def cache_database(tmp_path, monkeypatch):
    """Separate engines/sessions, as used by independent worker processes."""

    url = os.environ.get("REFERENCE_CACHE_TEST_DATABASE_URL")
    admin_engine = None
    schema = None
    if url:
        # Explicit opt-in to a test database. Never use application DATABASE_URL.
        admin_engine = create_database_engine(url)
        assert admin_engine.dialect.name == "postgresql"
        schema = f"test_reference_cache_{uuid.uuid4().hex}"
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
    else:
        url = f"sqlite+pysqlite:///{(tmp_path / 'reference-cache.sqlite3').as_posix()}"
    engines = []
    clock = [NOW]
    monkeypatch.setattr(SharedIndexCache, "_now", staticmethod(lambda session: clock[0]))

    def connect():
        engine = create_database_engine(url)
        if schema is not None:
            engine = engine.execution_options(schema_translate_map={None: schema})
        if not engines:
            Base.metadata.create_all(engine)
        engines.append(engine)
        return create_session_factory(engine)

    yield connect, clock
    for engine in engines:
        engine.dispose()
    if admin_engine is not None:
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


@pytest.fixture(params=["accounts", "contacts"])
def board_kind(request):
    return request.param


def make_service(kind, factory, client, *, scope="source-a", board_id=None, ttl=300):
    if kind == "accounts":
        service_class = AccountsIndexService
        default_board_id = BOARD_CONTRACT.accounts_board_id
    else:
        service_class = ContactsIndexService
        default_board_id = BOARD_CONTRACT.contacts_board_id
    return service_class(
        client=client,
        board_id=board_id or default_board_id,
        cache_ttl_seconds=ttl,
        shared_cache=SharedIndexCache(session_factory=factory, source_scope=scope),
    )


def make_client(kind, *, two_pages=True, extra_item=False):
    record = _account if kind == "accounts" else _contact_item
    pages = {
        None: {"cursor": "last" if two_pages else None, "items": [record("10")]},
    }
    if two_pages:
        pages["last"] = {"cursor": None, "items": [record("20")]}
    if extra_item:
        pages["last" if two_pages else None]["items"].append(record("30"))
    client_class = FakeAccountsClient if kind == "accounts" else FakeContactsClient
    return client_class(pages)


def records(index):
    return index.accounts if hasattr(index, "accounts") else index.contacts


def test_workers_and_restarts_reuse_all_pages_without_extending_ttl(cache_database, board_kind):
    connect, clock = cache_database
    first_client = make_client(board_kind)
    first = make_service(board_kind, connect(), first_client).load_index()
    second_client = make_client(board_kind, extra_item=True)
    second_factory = connect()
    second_worker = make_service(board_kind, second_factory, second_client)

    clock[0] = NOW + timedelta(seconds=299)
    assert second_worker.load_index() == first
    assert first_client.page_calls == [None, "last"]
    assert second_client.page_calls == []
    # A restarted worker cannot reset freshness to five minutes from its read.
    clock[0] = NOW + timedelta(seconds=300)
    restarted = make_service(board_kind, connect(), second_client)
    refreshed = restarted.load_index()
    assert [record.item_id for record in records(refreshed)] == ["10", "20", "30"]
    assert second_client.page_calls == [None, "last"]
    # A worker which already read the old index sees the replacement too.
    assert second_worker.load_index() == refreshed
    assert second_client.page_calls == [None, "last"]


def test_empty_board_is_a_valid_shared_snapshot(cache_database, board_kind):
    connect, _ = cache_database
    client = make_client(board_kind, two_pages=False)
    client.pages[None]["items"] = []
    first = make_service(board_kind, connect(), client).load_index()
    second_client = make_client(board_kind)
    assert make_service(board_kind, connect(), second_client).load_index() == first
    assert records(first) == ()
    assert second_client.page_calls == []


def test_force_refresh_and_clear_are_visible_to_other_workers(cache_database, board_kind):
    connect, _ = cache_database
    first_client = make_client(board_kind, two_pages=False)
    first_worker = make_service(board_kind, connect(), first_client)
    first_worker.load_index()
    second_client = make_client(board_kind, extra_item=True)
    second_worker = make_service(board_kind, connect(), second_client)
    refreshed = second_worker.load_index(force_refresh=True)
    assert first_worker.load_index() == refreshed
    assert first_client.page_calls == [None]

    second_worker.clear_cache()
    assert len(records(first_worker.load_index())) == 1
    assert first_client.page_calls == [None, None]
    assert second_worker.load_index() == first_worker.load_index()
    assert second_client.page_calls == [None, "last"]


@pytest.mark.parametrize("failure", ["network", "malformed", "duplicate", "cursor_loop"])
def test_failed_refresh_keeps_old_snapshot_expired_then_recovers(
    cache_database, board_kind, failure
):
    connect, clock = cache_database
    factory = connect()
    first = make_service(board_kind, factory, make_client(board_kind)).load_index()
    with factory() as session:
        old = session.scalars(select(ReferenceIndexSnapshot)).one()
        old_payload, old_refreshed_at = old.payload_json, old.refreshed_at
    clock[0] += timedelta(seconds=300)
    broken_client = make_client(board_kind, extra_item=True)
    if failure == "network":
        # KeyError on page two simulates failure after a successful first page.
        del broken_client.pages["last"]
        expected = KeyError
    else:
        expected = AccountsContractError
        if failure == "malformed":
            broken_client.pages["last"]["items"] = [None]
        elif failure == "duplicate":
            broken_client.pages["last"]["items"] = broken_client.pages[None]["items"]
        else:
            broken_client.pages["last"]["cursor"] = "last"
    broken = make_service(board_kind, connect(), broken_client)
    for _ in range(2):
        with pytest.raises(expected):
            broken.load_index()
    with factory() as session:
        retained = session.scalars(select(ReferenceIndexSnapshot)).one()
        assert retained.payload_json == old_payload
        assert retained.refreshed_at == old_refreshed_at
    assert broken_client.page_calls == [None, "last", None, "last"]
    recovered = make_service(board_kind, connect(), make_client(board_kind, extra_item=True))
    assert len(records(recovered.load_index())) == 3
    assert len(records(first)) == 2


@pytest.mark.parametrize("expired", [False, True])
def test_concurrent_workers_share_one_complete_refresh(cache_database, board_kind, expired):
    connect, clock = cache_database
    factory_one, factory_two = connect(), connect()
    if expired:
        make_service(board_kind, factory_one, make_client(board_kind)).load_index()
        clock[0] += timedelta(seconds=300)
    owner_client = make_client(board_kind, extra_item=True)
    waiter_client = make_client(board_kind, two_pages=False)
    owner = make_service(board_kind, factory_one, owner_client)
    waiter = make_service(board_kind, factory_two, waiter_client)
    entered_last_page, release_last_page, waiter_started = Event(), Event(), Event()
    method_name = "load_accounts_page" if board_kind == "accounts" else "load_contacts_page"
    original = getattr(owner_client, method_name)

    def slow_page(board_id, *, cursor=None, limit=500):
        if cursor == "last":
            entered_last_page.set()
            assert release_last_page.wait(5), "test did not release the last page"
        return original(board_id, cursor=cursor, limit=limit)

    setattr(owner_client, method_name, slow_page)

    @event.listens_for(factory_two.kw["bind"], "before_cursor_execute")
    def observe_waiter(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO") and "reference_index_snapshots" in statement:
            waiter_started.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(owner.load_index)
        try:
            assert entered_last_page.wait(5)
            second = workers.submit(waiter.load_index)
            assert waiter_started.wait(5)
            assert not second.done()
        finally:
            release_last_page.set()
        assert first.result(timeout=5) == second.result(timeout=5)
        assert len(records(second.result())) == 3
    assert owner_client.page_calls == [None, "last"]
    assert waiter_client.page_calls == []


def test_cache_isolates_permission_scopes_and_board_kinds(cache_database):
    connect, _ = cache_database
    factory = connect()
    a = make_service("accounts", factory, make_client("accounts"))
    b_client = make_client("accounts", two_pages=False)
    b = make_service("accounts", connect(), b_client, scope="different-permissions")
    contacts_client = make_client("contacts")
    c = make_service("contacts", connect(), contacts_client)
    a.load_index()
    b.load_index()
    c.load_index()
    assert b_client.page_calls == [None]
    assert contacts_client.page_calls == [None, "last"]
    with factory() as session:
        assert len(session.scalars(select(ReferenceIndexSnapshot)).all()) == 3


def test_cache_isolates_board_ids(cache_database):
    connect, _ = cache_database
    factory = connect()
    make_service("accounts", factory, make_client("accounts")).load_index()
    client = make_client("accounts", two_pages=False)
    # This fake reader accepts the alternate board for this isolation check.
    client.load_accounts_page = lambda board_id, **kwargs: client.pages[kwargs["cursor"]]
    other = make_service("accounts", connect(), client, board_id=123)
    assert len(other.load_index().accounts) == 1
    with factory() as session:
        assert {row.board_id for row in session.scalars(select(ReferenceIndexSnapshot))} == {
            str(BOARD_CONTRACT.accounts_board_id), "123"
        }


def test_failed_first_refresh_rolls_back_and_next_worker_can_refresh(cache_database, board_kind):
    connect, _ = cache_database
    factory = connect()
    client = make_client(board_kind)
    del client.pages["last"]
    with pytest.raises(KeyError):
        make_service(board_kind, factory, client).load_index()
    with factory() as session:
        assert session.scalars(select(ReferenceIndexSnapshot)).all() == []
    recovered = make_service(board_kind, connect(), make_client(board_kind))
    assert len(records(recovered.load_index())) == 2


def test_zero_ttl_always_fetches_complete_board(cache_database, board_kind):
    connect, _ = cache_database
    client = make_client(board_kind)
    service = make_service(board_kind, connect(), client, ttl=0)
    service.load_index()
    service.load_index()
    assert client.page_calls == [None, "last", None, "last"]


@pytest.mark.parametrize("changed", ["api_url", "api_version", "access_token"])
def test_scope_tracks_source_and_credentials_without_exposing_them(changed):
    values = dict(api_url="https://api.monday.com/v2", api_version="2026-07", access_token="SECRET")
    first = reference_cache_scope(**values)
    values[changed] += "-changed"
    second = reference_cache_scope(**values)
    assert first != second
    assert len(first) == 64
    assert "SECRET" not in first + second


def test_expiry_detects_new_competing_account_in_final_page(cache_database):
    connect, clock = cache_database
    client = make_client("accounts", two_pages=False)
    factory = connect()
    first_worker = make_service("accounts", factory, client)
    requester = RequesterIdentity(
        email_address="person@example.co.uk",
        domain="example.co.uk",
        company=None,
        source="top_level_sender",
    )
    assert match_account(first_worker.load_index(), requester).resolution == AccountResolution.MATCHED
    clock[0] += timedelta(seconds=300)
    second_worker = make_service("accounts", connect(), make_client("accounts"))
    assert len(second_worker.load_index().accounts) == 2
    result = match_account(first_worker.load_index(), requester)
    assert result.resolution == AccountResolution.UNRESOLVED
    assert result.domain_candidate_ids == ("10", "20")


def test_expiry_detects_new_competing_contact_and_persists_only_email_hashes(cache_database):
    connect, clock = cache_database
    accounts = make_service("accounts", connect(), make_client("accounts")).load_index()
    client = make_client("contacts", two_pages=False)
    factory = connect()
    first_worker = make_service("contacts", factory, client)
    requester = RequesterIdentity(
        email_address="requester@example.co.uk",
        email_address_sha256=email_address_sha256("requester@example.co.uk"),
        domain="example.co.uk",
        company=None,
        source="top_level_sender",
    )
    assert match_account(accounts, requester, contacts=first_worker.load_index()).resolution == AccountResolution.MATCHED
    with factory() as session:
        snapshot = session.scalars(
            select(ReferenceIndexSnapshot).where(ReferenceIndexSnapshot.index_kind == "contacts")
        ).one()
        assert "requester@example.co.uk" not in str(snapshot.payload_json)
        assert requester.email_address_sha256 in str(snapshot.payload_json)
    clock[0] += timedelta(seconds=300)
    make_service("contacts", connect(), make_client("contacts")).load_index()
    result = match_account(accounts, requester, contacts=first_worker.load_index())
    assert result.resolution == AccountResolution.UNRESOLVED
    assert result.contact_candidate_ids == ("10", "20")
