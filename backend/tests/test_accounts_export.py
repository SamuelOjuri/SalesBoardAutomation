from collections.abc import Mapping
from typing import Any

from app.accounts_export import load_export_payload
from app.config import BOARD_CONTRACT


def _account(
    item_id: str,
    *,
    state: str = "active",
    duplicate_label_ids: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": f"Account {item_id}",
        "state": state,
        "board": {"id": str(BOARD_CONTRACT.accounts_board_id)},
        "column_values": [
            {
                "id": BOARD_CONTRACT.account_email_domain_column_id,
                "type": "text",
                "text": f"account-{item_id}.example",
            },
            {
                "id": BOARD_CONTRACT.account_duplicate_column_id,
                "type": "dropdown",
                "values": [
                    {"id": label_id}
                    for label_id in (duplicate_label_ids or [])
                ],
            },
        ],
    }


class FakeAccountsReader:
    def __init__(self, pages: dict[str | None, Mapping[str, Any]]) -> None:
        self.pages = pages
        self.page_calls: list[str | None] = []

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
        raise AssertionError("the full export must not fetch individual items")


def test_export_follows_cursors_until_null_and_includes_every_account() -> None:
    client = FakeAccountsReader(
        {
            None: {
                "cursor": "page-2",
                "items": [_account("1")],
            },
            "page-2": {
                "cursor": None,
                "items": [
                    _account("2", state="archived"),
                    _account("3", duplicate_label_ids=[1]),
                ],
            },
        }
    )

    payload = load_export_payload(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
    )

    assert client.page_calls == [None, "page-2"]
    assert payload["count"] == 3
    assert [account["id"] for account in payload["accounts"]] == ["1", "2", "3"]
    assert [account["eligible"] for account in payload["accounts"]] == [
        True,
        False,
        False,
    ]


def test_export_can_limit_output_to_eligible_accounts() -> None:
    client = FakeAccountsReader(
        {
            None: {
                "cursor": None,
                "items": [
                    _account("1"),
                    _account("2", duplicate_label_ids=[1]),
                ],
            }
        }
    )

    payload = load_export_payload(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
        eligible_only=True,
    )

    assert payload["eligibleOnly"] is True
    assert payload["count"] == 1
    assert [account["id"] for account in payload["accounts"]] == ["1"]