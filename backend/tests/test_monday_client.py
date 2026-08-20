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