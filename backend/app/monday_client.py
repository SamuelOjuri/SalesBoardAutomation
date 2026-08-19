"""Minimal Monday GraphQL client used by the startup schema gate."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import BOARD_CONTRACT, Settings


class MondayAPIError(RuntimeError):
    """Raised for a malformed or rejected Monday GraphQL response."""


class MondayTransientError(MondayAPIError):
    """Raised for retryable Monday HTTP responses."""


class MondayClient:
    def __init__(
        self,
        *,
        access_token: str,
        api_version: str,
        api_url: str = "https://api.monday.com/v2",
        request_timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._api_url = api_url
        self._request_timeout_seconds = request_timeout_seconds
        self._max_attempts = max_attempts
        self._session.headers.update(
            {
                "Authorization": access_token,
                "API-Version": api_version,
                "Content-Type": "application/json",
            }
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> MondayClient:
        return cls(
            access_token=settings.monday_ingestion_access_token.get_secret_value(),
            api_version=settings.monday_api_version,
            api_url=settings.monday_api_url,
            request_timeout_seconds=settings.monday_request_timeout_seconds,
            max_attempts=settings.monday_request_max_attempts,
        )

    def load_sales_board_schema(self, board_id: int) -> Mapping[str, Any]:
        query = """
            query SalesBoardSchema($board_ids: [ID!]!, $column_ids: [String!]!) {
                boards(ids: $board_ids) {
                    id
                    columns(ids: $column_ids) {
                        id
                        type
                        settings
                    }
                }
            }
        """
        payload = self._execute(
            query,
            {
                "board_ids": [str(board_id)],
                "column_ids": [
                    BOARD_CONTRACT.email_file_column_id,
                    BOARD_CONTRACT.accounts_relation_column_id,
                    BOARD_CONTRACT.postcode_column_id,
                ],
            },
        )
        boards = payload.get("boards")
        if not isinstance(boards, list) or len(boards) != 1:
            raise MondayAPIError("Monday returned an unexpected Sales board count")
        board = boards[0]
        if not isinstance(board, Mapping):
            raise MondayAPIError("Monday returned a malformed Sales board")
        return board

    def _execute(
        self, query: str, variables: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type(
                (requests.ConnectionError, requests.Timeout, MondayTransientError)
            ),
            reraise=True,
        )
        return retrying(self._post_once, query, variables)

    def _post_once(
        self, query: str, variables: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        response = self._session.post(
            self._api_url,
            json={"query": query, "variables": dict(variables)},
            timeout=self._request_timeout_seconds,
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise MondayTransientError(
                f"Monday request failed with retryable status {response.status_code}"
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise MondayAPIError(
                f"Monday request failed with status {response.status_code}"
            ) from error

        try:
            payload = response.json()
        except ValueError as error:
            raise MondayAPIError("Monday returned a non-JSON response") from error
        if not isinstance(payload, Mapping):
            raise MondayAPIError("Monday returned a malformed GraphQL response")
        if payload.get("errors"):
            raise MondayAPIError("Monday returned GraphQL errors")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MondayAPIError("Monday response did not contain GraphQL data")
        return data

    def close(self) -> None:
        if self._owns_session:
            self._session.close()