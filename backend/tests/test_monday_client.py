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