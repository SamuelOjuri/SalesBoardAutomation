"""Minimal Monday GraphQL client used by the startup schema gate."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import BOARD_CONTRACT, Settings


_ACCOUNT_ITEM_FIELDS = """
    id
    name
    state
    board { id }
    column_values(ids: $column_ids) {
        id
        type
        value
        ... on TextValue {
            text
        }
        ... on DropdownValue {
            values {
                id
                label
            }
        }
    }
"""

_SALES_PUBLICATION_COLUMN_IDS = frozenset(
    {
        BOARD_CONTRACT.postcode_column_id,
        BOARD_CONTRACT.accounts_relation_column_id,
    }
)


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
        asset_session: requests.Session | None = None,
    ) -> None:
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._asset_session = asset_session or requests.Session()
        self._owns_asset_session = asset_session is None
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

    def load_sales_item_intake(self, item_id: str) -> Mapping[str, Any]:
        query = """
            query SalesItemIntake($item_ids: [ID!]!, $column_ids: [String!]!) {
                items(ids: $item_ids) {
                    id
                    state
                    board { id }
                    group { id title }
                    assets {
                        id
                        name
                        file_size
                        created_at
                        url
                        public_url
                    }
                    column_values(ids: $column_ids) {
                        id
                        type
                        value
                    }
                }
            }
        """
        payload = self._execute(
            query,
            {
                "item_ids": [str(item_id)],
                "column_ids": [BOARD_CONTRACT.email_file_column_id],
            },
        )
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise MondayAPIError("Monday returned an unexpected Sales item count")
        item = items[0]
        if not isinstance(item, Mapping):
            raise MondayAPIError("Monday returned a malformed Sales item")
        return item

    def load_sales_item_for_publication(
        self, item_id: str
    ) -> Mapping[str, Any]:
        query = """
            query SalesItemPublication(
                $item_ids: [ID!]!,
                $column_ids: [String!]!
            ) {
                items(ids: $item_ids) {
                    id
                    state
                    board { id }
                    group { id title }
                    assets {
                        id
                        name
                        file_size
                        created_at
                        url
                        public_url
                    }
                    column_values(ids: $column_ids) {
                        id
                        type
                        value
                        ... on DropdownValue {
                            values {
                                id
                                label
                            }
                        }
                        ... on BoardRelationValue {
                            linked_item_ids
                        }
                    }
                }
            }
        """
        payload = self._execute(
            query,
            {
                "item_ids": [str(item_id)],
                "column_ids": [
                    BOARD_CONTRACT.email_file_column_id,
                    BOARD_CONTRACT.postcode_column_id,
                    BOARD_CONTRACT.accounts_relation_column_id,
                ],
            },
        )
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise MondayAPIError("Monday returned an unexpected Sales item count")
        item = items[0]
        if not isinstance(item, Mapping):
            raise MondayAPIError("Monday returned a malformed Sales item")
        return item

    def change_sales_item_column_values(
        self,
        board_id: int,
        item_id: str,
        column_values: Mapping[str, object],
    ) -> None:
        if board_id != BOARD_CONTRACT.sales_board_id:
            raise ValueError("writes are restricted to the configured Sales board")
        if not column_values:
            raise ValueError("column_values must not be empty")
        if not set(column_values).issubset(_SALES_PUBLICATION_COLUMN_IDS):
            raise ValueError("column_values contains a non-allow-listed column")
        self._validate_publication_values(column_values)

        query = """
            mutation ChangeSalesItemColumns(
                $board_id: ID!,
                $item_id: ID!,
                $column_values: JSON!
            ) {
                change_multiple_column_values(
                    board_id: $board_id,
                    item_id: $item_id,
                    column_values: $column_values
                ) {
                    id
                }
            }
        """
        payload = self._post_once(
            query,
            {
                "board_id": str(board_id),
                "item_id": str(item_id),
                "column_values": json.dumps(
                    column_values,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        )
        changed_item = payload.get("change_multiple_column_values")
        if not isinstance(changed_item, Mapping) or str(
            changed_item.get("id", "")
        ) != str(item_id):
            raise MondayAPIError("Monday returned a malformed changed Sales item")

    @staticmethod
    def _validate_publication_values(
        column_values: Mapping[str, object],
    ) -> None:
        postcode_value = column_values.get(BOARD_CONTRACT.postcode_column_id)
        if postcode_value is not None and not _is_single_positive_id_value(
            postcode_value, "ids"
        ):
            raise ValueError("Postcode publication value must contain one label ID")

        accounts_value = column_values.get(
            BOARD_CONTRACT.accounts_relation_column_id
        )
        if accounts_value is not None and not _is_single_positive_id_value(
            accounts_value, "item_ids"
        ):
            raise ValueError("Accounts publication value must contain one item ID")

    def load_postcode_dropdown_column(self, board_id: int) -> Mapping[str, Any]:
        board = self.load_sales_board_schema(board_id)
        columns = board.get("columns")
        if not isinstance(columns, list):
            raise MondayAPIError("Monday returned malformed Sales board columns")
        matches = [
            column
            for column in columns
            if isinstance(column, Mapping)
            and column.get("id") == BOARD_CONTRACT.postcode_column_id
        ]
        if len(matches) != 1 or matches[0].get("type") != "dropdown":
            raise MondayAPIError("Monday Postcode dropdown is unavailable")
        if not isinstance(matches[0].get("settings"), Mapping):
            raise MondayAPIError("Monday Postcode dropdown settings are unavailable")
        return matches[0]

    def load_accounts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        variables: dict[str, Any] = {
            "column_ids": [
                BOARD_CONTRACT.account_email_domain_column_id,
                BOARD_CONTRACT.account_duplicate_column_id,
            ],
            "limit": limit,
        }
        if cursor is None:
            query = f"""
                query AccountsPage(
                    $board_ids: [ID!]!,
                    $column_ids: [String!]!,
                    $limit: Int!
                ) {{
                    boards(ids: $board_ids) {{
                        id
                        items_page(limit: $limit) {{
                            cursor
                            items {{
                                {_ACCOUNT_ITEM_FIELDS}
                            }}
                        }}
                    }}
                }}
            """
            variables["board_ids"] = [str(board_id)]
            payload = self._execute(query, variables)
            boards = payload.get("boards")
            if not isinstance(boards, list) or len(boards) != 1:
                raise MondayAPIError("Monday returned an unexpected Accounts board count")
            board = boards[0]
            if not isinstance(board, Mapping):
                raise MondayAPIError("Monday returned a malformed Accounts board")
            page = board.get("items_page")
        else:
            if not cursor.strip():
                raise ValueError("cursor must not be empty")
            query = f"""
                query AccountsNextPage(
                    $cursor: String!,
                    $column_ids: [String!]!,
                    $limit: Int!
                ) {{
                    next_items_page(cursor: $cursor, limit: $limit) {{
                        cursor
                        items {{
                            {_ACCOUNT_ITEM_FIELDS}
                        }}
                    }}
                }}
            """
            variables["cursor"] = cursor
            payload = self._execute(query, variables)
            page = payload.get("next_items_page")
        if not isinstance(page, Mapping):
            raise MondayAPIError("Monday returned a malformed Accounts page")
        return page

    def load_account_item(self, item_id: str) -> Mapping[str, Any] | None:
        query = f"""
            query AccountItem($item_ids: [ID!]!, $column_ids: [String!]!) {{
                items(ids: $item_ids) {{
                    {_ACCOUNT_ITEM_FIELDS}
                }}
            }}
        """
        payload = self._execute(
            query,
            {
                "item_ids": [str(item_id)],
                "column_ids": [
                    BOARD_CONTRACT.account_email_domain_column_id,
                    BOARD_CONTRACT.account_duplicate_column_id,
                ],
            },
        )
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > 1:
            raise MondayAPIError("Monday returned an unexpected Account item count")
        if not items:
            return None
        item = items[0]
        if not isinstance(item, Mapping):
            raise MondayAPIError("Monday returned a malformed Account item")
        return item

    def download_asset(
        self,
        url: str,
        destination: Path,
        *,
        expected_size: int,
        expected_sha256: str | None = None,
    ) -> str:
        if expected_size < 0:
            raise ValueError("expected_size must not be negative")
        if expected_sha256 is not None:
            expected_sha256 = expected_sha256.casefold()
            if len(expected_sha256) != 64 or any(
                character not in "0123456789abcdef"
                for character in expected_sha256
            ):
                raise ValueError("expected_sha256 must be a hexadecimal SHA-256")

        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type(
                (requests.ConnectionError, requests.Timeout, MondayTransientError)
            ),
            reraise=True,
        )
        return retrying(
            self._download_once,
            url,
            destination,
            expected_size,
            expected_sha256,
        )

    def _download_once(
        self,
        url: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str | None,
    ) -> str:
        try:
            response = self._asset_session.get(
                url,
                headers={"Accept": "*/*"},
                stream=True,
                timeout=self._request_timeout_seconds,
            )
            try:
                if response.status_code == 429 or response.status_code >= 500:
                    raise MondayTransientError(
                        "Monday asset download failed with retryable status "
                        f"{response.status_code}"
                    )
                try:
                    response.raise_for_status()
                except requests.HTTPError as error:
                    raise MondayAPIError(
                        "Monday asset download failed with status "
                        f"{response.status_code}"
                    ) from error

                digest = hashlib.sha256()
                downloaded_size = 0
                with destination.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        downloaded_size += len(chunk)
                        if downloaded_size > expected_size:
                            raise MondayTransientError(
                                "Monday asset download exceeded its expected size"
                            )
                        digest.update(chunk)
                        output.write(chunk)
            finally:
                response.close()

            if downloaded_size != expected_size:
                raise MondayTransientError(
                    "Monday asset download size did not match its metadata"
                )
            actual_sha256 = digest.hexdigest()
            if expected_sha256 is not None and not hmac.compare_digest(
                actual_sha256, expected_sha256
            ):
                raise MondayTransientError(
                    "Monday asset download SHA-256 did not match"
                )
            return actual_sha256
        except Exception:
            destination.unlink(missing_ok=True)
            raise

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
        if self._owns_asset_session:
            self._asset_session.close()


def _is_single_positive_id_value(value: object, key: str) -> bool:
    if not isinstance(value, Mapping) or set(value) != {key}:
        return False
    identifiers = value.get(key)
    if not isinstance(identifiers, list) or len(identifiers) != 1:
        return False
    identifier = identifiers[0]
    if identifier is None or isinstance(identifier, bool):
        return False
    normalized = str(identifier).strip()
    return normalized.isdecimal() and int(normalized) > 0
