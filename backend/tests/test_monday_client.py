import hashlib
from pathlib import Path
from typing import Any, cast

import requests

from app.config import BOARD_CONTRACT
from app.monday_client import MondayClient


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.headers: dict[str, str] = {}
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payload)


class QueuedFakeSession(FakeSession):
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        super().__init__({})
        self.payloads = payloads

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payloads.pop(0))


class FakeDownloadResponse:
    status_code = 200

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return [
            self.content[index : index + chunk_size]
            for index in range(0, len(self.content), chunk_size)
        ]

    def close(self) -> None:
        self.closed = True


class FakeDownloadSession(FakeSession):
    def __init__(self, content: bytes) -> None:
        super().__init__({})
        self.response = FakeDownloadResponse(content)

    def get(self, url: str, **kwargs: Any) -> FakeDownloadResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


def test_schema_loader_requests_only_typed_contract_columns() -> None:
    board = {"id": str(BOARD_CONTRACT.sales_board_id), "columns": []}
    fake_session = FakeSession({"data": {"boards": [board]}})
    client = MondayClient(
        access_token="token",
        api_version="2026-07",
        session=cast(requests.Session, fake_session),
    )

    assert client.load_sales_board_schema(BOARD_CONTRACT.sales_board_id) == board

    call = fake_session.calls[0]
    assert call["json"]["variables"] == {
        "board_ids": [str(BOARD_CONTRACT.sales_board_id)],
        "column_ids": [
            BOARD_CONTRACT.email_file_column_id,
            BOARD_CONTRACT.accounts_relation_column_id,
            BOARD_CONTRACT.postcode_column_id,
        ],
    }
    assert "settings" in call["json"]["query"]
    assert "settings_str" not in call["json"]["query"]
    assert fake_session.headers["Authorization"] == "token"
    assert fake_session.headers["API-Version"] == "2026-07"


def test_intake_loader_requests_authoritative_file_membership_and_assets() -> None:
    item = {"id": "42", "assets": [], "column_values": []}
    fake_session = FakeSession({"data": {"items": [item]}})
    client = MondayClient(
        access_token="token",
        api_version="2026-07",
        session=cast(requests.Session, fake_session),
    )

    assert client.load_sales_item_intake("42") == item

    call = fake_session.calls[0]
    assert call["json"]["variables"] == {
        "item_ids": ["42"],
        "column_ids": [BOARD_CONTRACT.email_file_column_id],
    }
    assert "assets" in call["json"]["query"]
    assert "column_values" in call["json"]["query"]


def test_postcode_loader_returns_live_typed_dropdown_settings() -> None:
    postcode_column = {
        "id": BOARD_CONTRACT.postcode_column_id,
        "type": "dropdown",
        "settings": {"labels": [{"id": 115, "name": "WA"}]},
    }
    board = {
        "id": str(BOARD_CONTRACT.sales_board_id),
        "columns": [postcode_column],
    }
    fake_session = FakeSession({"data": {"boards": [board]}})
    client = MondayClient(
        access_token="token",
        api_version="2026-07",
        session=cast(requests.Session, fake_session),
    )

    assert (
        client.load_postcode_dropdown_column(BOARD_CONTRACT.sales_board_id)
        == postcode_column
    )
    assert "settings" in fake_session.calls[0]["json"]["query"]


def test_accounts_loader_uses_typed_values_and_continues_from_cursor() -> None:
    fake_session = QueuedFakeSession(
        [
            {
                "data": {
                    "boards": [
                        {
                            "id": str(BOARD_CONTRACT.accounts_board_id),
                            "items_page": {"cursor": "next", "items": []},
                        }
                    ]
                }
            },
            {"data": {"next_items_page": {"cursor": None, "items": []}}},
        ]
    )
    client = MondayClient(
        access_token="token",
        api_version="2026-07",
        session=cast(requests.Session, fake_session),
    )

    first_page = client.load_accounts_page(BOARD_CONTRACT.accounts_board_id)
    final_page = client.load_accounts_page(
        BOARD_CONTRACT.accounts_board_id, cursor=first_page["cursor"]
    )

    assert final_page["cursor"] is None
    first_call, second_call = fake_session.calls
    assert first_call["json"]["variables"] == {
        "board_ids": [str(BOARD_CONTRACT.accounts_board_id)],
        "column_ids": [
            BOARD_CONTRACT.account_email_domain_column_id,
            BOARD_CONTRACT.account_duplicate_column_id,
        ],
        "limit": 500,
    }
    assert second_call["json"]["variables"]["cursor"] == "next"
    assert "next_items_page" in second_call["json"]["query"]
    assert "... on DropdownValue" in first_call["json"]["query"]
    assert "values" in first_call["json"]["query"]


def test_selected_account_loader_returns_none_when_item_disappears() -> None:
    fake_session = FakeSession({"data": {"items": []}})
    client = MondayClient(
        access_token="token",
        api_version="2026-07",
        session=cast(requests.Session, fake_session),
    )

    assert client.load_account_item("42") is None
    assert fake_session.calls[0]["json"]["variables"]["item_ids"] == ["42"]
    assert "state" in fake_session.calls[0]["json"]["query"]


def test_asset_download_streams_and_validates_size_and_sha256(tmp_path: Path) -> None:
    content = b"From: requester@example.com\n"
    fake_session = FakeDownloadSession(content)
    client = MondayClient(
        access_token="token",
        api_version="2026-07",
        session=cast(requests.Session, fake_session),
    )
    destination = tmp_path / "asset.eml"
    expected_digest = hashlib.sha256(content).hexdigest()

    digest = client.download_asset(
        "https://files.monday.com/asset.eml",
        destination,
        expected_size=len(content),
        expected_sha256=expected_digest,
    )

    assert digest == expected_digest
    assert destination.read_bytes() == content
    assert fake_session.response.closed is True
    assert fake_session.calls[0]["stream"] is True