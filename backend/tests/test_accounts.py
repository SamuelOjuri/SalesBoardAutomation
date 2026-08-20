from collections.abc import Mapping
from typing import Any

import pytest

from app.config import BOARD_CONTRACT
from app.services.accounts import (
    AccountsContractError,
    AccountsIndexService,
    parse_account_item,
)


def _account(
    item_id: str,
    *,
    state: str = "active",
    domain: str | None = "example.co.uk",
    duplicate_values: object = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": f"Account {item_id}",
        "state": state,
        "column_values": [
            {
                "id": BOARD_CONTRACT.account_email_domain_column_id,
                "type": "text",
                "text": domain,
            },
            {
                "id": BOARD_CONTRACT.account_duplicate_column_id,
                "type": "dropdown",
                "values": duplicate_values,
            },
        ],
    }


class FakeAccountsClient:
    def __init__(
        self,
        pages: dict[str | None, Mapping[str, Any]],
        *,
        selected_item: Mapping[str, Any] | None = None,
    ) -> None:
        self.pages = pages
        self.selected_item = selected_item
        self.page_calls: list[str | None] = []
        self.item_calls: list[str] = []

    def load_accounts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        assert board_id == BOARD_CONTRACT.accounts_board_id
        assert limit == 500
        self.page_calls.append(cursor)
        return self.pages[cursor]

    def load_account_item(self, item_id: str) -> Mapping[str, Any] | None:
        self.item_calls.append(item_id)
        return self.selected_item


def test_index_paginates_to_null_filters_only_duplicate_label_one_and_caches() -> None:
    client = FakeAccountsClient(
        {
            None: {
                "cursor": "next-page",
                "items": [
                    _account("1", duplicate_values=None),
                    _account("2", duplicate_values=[{"id": "1"}]),
                ],
            },
            "next-page": {
                "cursor": None,
                "items": [
                    _account("3", duplicate_values=[]),
                    _account("4", duplicate_values=[{"id": 2}]),
                    _account("5", state="archived", duplicate_values=[]),
                ],
            },
        }
    )
    now = [100.0]
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
        clock=lambda: now[0],
    )

    first = service.load_index()
    second = service.load_index()

    assert first is second
    assert client.page_calls == [None, "next-page"]
    assert [account.item_id for account in first.eligible_accounts] == ["1", "3", "4"]
    assert first.get("2").duplicate is True  # type: ignore[union-attr]
    assert first.get("4").duplicate is False  # type: ignore[union-attr]


def test_expired_index_fetches_every_page_again() -> None:
    client = FakeAccountsClient({None: {"cursor": None, "items": []}})
    now = [100.0]
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
        cache_ttl_seconds=300,
        clock=lambda: now[0],
    )

    service.load_index()
    now[0] = 401.0
    service.load_index()

    assert client.page_calls == [None, None]


def test_selected_account_is_refetched_and_must_still_be_eligible() -> None:
    client = FakeAccountsClient(
        {None: {"cursor": None, "items": [_account("42", duplicate_values=[])]}},
        selected_item=_account("42", duplicate_values=[{"id": 1}]),
    )
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
    )
    assert service.load_index().get("42") is not None

    assert service.revalidate_selected_account("42") is None
    assert client.item_calls == ["42"]


@pytest.mark.parametrize(
    ("raw_value", "expected_ids"),
    [(None, ()), ('{"ids":[]}', ()), ('{"ids":[1]}', (1,))],
)
def test_legacy_duplicate_json_semantics_are_fail_closed(
    raw_value: str | None, expected_ids: tuple[int, ...]
) -> None:
    raw_item = _account("10")
    duplicate_column = raw_item["column_values"][1]
    duplicate_column.pop("values")
    duplicate_column["value"] = raw_value

    assert parse_account_item(raw_item).duplicate_label_ids == expected_ids


def test_malformed_duplicate_value_rejects_the_index() -> None:
    raw_item = _account("10", duplicate_values="selected")
    client = FakeAccountsClient(
        {None: {"cursor": None, "items": [raw_item]}}
    )
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
    )

    with pytest.raises(AccountsContractError, match="Duplicate values"):
        service.load_index()