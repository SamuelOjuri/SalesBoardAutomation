"""Fully paginated, duplicate-safe Accounts board index."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.behavioral_contract import BEHAVIORAL_CONTRACT
from app.config import BOARD_CONTRACT
from app.services.requester_identity import normalize_domain


class AccountsContractError(ValueError):
    """Raised when Monday returns an unsafe Accounts data shape."""


class AccountsReader(Protocol):
    def load_accounts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]: ...

    def load_account_item(self, item_id: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class AccountRecord:
    item_id: str
    name: str
    active: bool
    email_domain: str | None
    duplicate_label_ids: tuple[int, ...]

    @property
    def duplicate(self) -> bool:
        return (
            BEHAVIORAL_CONTRACT.duplicate_account_label_id
            in self.duplicate_label_ids
        )

    @property
    def eligible(self) -> bool:
        return self.active and not self.duplicate


@dataclass(frozen=True, slots=True)
class AccountsIndex:
    accounts: tuple[AccountRecord, ...]

    @property
    def eligible_accounts(self) -> tuple[AccountRecord, ...]:
        return tuple(account for account in self.accounts if account.eligible)

    def get(self, item_id: str) -> AccountRecord | None:
        return next(
            (account for account in self.accounts if account.item_id == str(item_id)),
            None,
        )


class AccountsIndexService:
    def __init__(
        self,
        *,
        client: AccountsReader,
        board_id: int,
        cache_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if board_id <= 0:
            raise ValueError("board_id must be positive")
        if cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must not be negative")
        self._client = client
        self._board_id = board_id
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cached_index: AccountsIndex | None = None
        self._cache_expires_at = 0.0

    def load_index(self, *, force_refresh: bool = False) -> AccountsIndex:
        now = self._clock()
        if (
            not force_refresh
            and self._cached_index is not None
            and now < self._cache_expires_at
        ):
            return self._cached_index

        with self._lock:
            now = self._clock()
            if (
                not force_refresh
                and self._cached_index is not None
                and now < self._cache_expires_at
            ):
                return self._cached_index
            index = self._fetch_all_pages()
            self._cached_index = index
            self._cache_expires_at = self._clock() + self._cache_ttl_seconds
            return index

    def revalidate_selected_account(self, item_id: str) -> AccountRecord | None:
        raw_item = self._client.load_account_item(str(item_id))
        if raw_item is None:
            return None
        account = parse_account_item(raw_item)
        if account.item_id != str(item_id):
            raise AccountsContractError("Monday returned the wrong Account item")
        return account if account.eligible else None

    def clear_cache(self) -> None:
        with self._lock:
            self._cached_index = None
            self._cache_expires_at = 0.0

    def _fetch_all_pages(self) -> AccountsIndex:
        accounts: list[AccountRecord] = []
        seen_item_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None

        while True:
            page = self._client.load_accounts_page(
                self._board_id,
                cursor=cursor,
                limit=500,
            )
            raw_items = page.get("items")
            if not isinstance(raw_items, list):
                raise AccountsContractError("Accounts page items must be a list")
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    raise AccountsContractError("Account item must be an object")
                account = parse_account_item(raw_item)
                if account.item_id in seen_item_ids:
                    raise AccountsContractError(
                        f"Monday returned duplicate Account item {account.item_id}"
                    )
                seen_item_ids.add(account.item_id)
                accounts.append(account)

            next_cursor = page.get("cursor")
            if next_cursor is None:
                return AccountsIndex(tuple(accounts))
            if not isinstance(next_cursor, str) or not next_cursor.strip():
                raise AccountsContractError("Accounts page cursor is malformed")
            if next_cursor in seen_cursors:
                raise AccountsContractError("Accounts pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor


def parse_account_item(raw_item: Mapping[str, Any]) -> AccountRecord:
    item_id = str(raw_item.get("id", "")).strip()
    if not item_id.isdecimal() or int(item_id) <= 0:
        raise AccountsContractError("Account item ID must be a positive decimal")
    name = raw_item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AccountsContractError(f"Account item {item_id} has no name")
    state = raw_item.get("state")
    if not isinstance(state, str):
        raise AccountsContractError(f"Account item {item_id} has no typed state")

    columns = raw_item.get("column_values")
    if not isinstance(columns, list):
        raise AccountsContractError(
            f"Account item {item_id} column_values must be a list"
        )
    email_column = _find_column(
        columns,
        BOARD_CONTRACT.account_email_domain_column_id,
        "text",
        item_id,
    )
    duplicate_column = _find_column(
        columns,
        BOARD_CONTRACT.account_duplicate_column_id,
        "dropdown",
        item_id,
    )
    return AccountRecord(
        item_id=item_id,
        name=" ".join(name.split()),
        active=state.casefold() == "active",
        email_domain=_parse_email_domain(email_column),
        duplicate_label_ids=_parse_duplicate_label_ids(duplicate_column, item_id),
    )


def _find_column(
    columns: list[Any],
    column_id: str,
    expected_type: str,
    item_id: str,
) -> Mapping[str, Any]:
    matches = [
        column
        for column in columns
        if isinstance(column, Mapping) and column.get("id") == column_id
    ]
    if len(matches) != 1 or matches[0].get("type") != expected_type:
        raise AccountsContractError(
            f"Account item {item_id} has an invalid {column_id} column"
        )
    return matches[0]


def _parse_email_domain(column: Mapping[str, Any]) -> str | None:
    raw_text = column.get("text")
    if raw_text is None and "value" in column:
        raw_text = _decode_json_value(column.get("value"))
    if raw_text is None or raw_text == "":
        return None
    return normalize_domain(raw_text)


def _parse_duplicate_label_ids(
    column: Mapping[str, Any], item_id: str
) -> tuple[int, ...]:
    if "values" in column:
        values = column.get("values")
        if values is None:
            return ()
        if not isinstance(values, list):
            raise AccountsContractError(
                f"Account item {item_id} Duplicate values must be a list"
            )
        raw_ids = [
            value.get("id") if isinstance(value, Mapping) else None
            for value in values
        ]
    else:
        decoded = _decode_json_value(column.get("value"))
        if decoded is None:
            return ()
        if not isinstance(decoded, Mapping) or not isinstance(
            decoded.get("ids"), list
        ):
            raise AccountsContractError(
                f"Account item {item_id} Duplicate value is malformed"
            )
        raw_ids = decoded["ids"]

    label_ids: list[int] = []
    for raw_id in raw_ids:
        if isinstance(raw_id, bool):
            raise AccountsContractError(
                f"Account item {item_id} Duplicate label ID is malformed"
            )
        try:
            label_id = int(raw_id)
        except (TypeError, ValueError) as error:
            raise AccountsContractError(
                f"Account item {item_id} Duplicate label ID is malformed"
            ) from error
        if label_id <= 0:
            raise AccountsContractError(
                f"Account item {item_id} Duplicate label ID is malformed"
            )
        if label_id not in label_ids:
            label_ids.append(label_id)
    return tuple(sorted(label_ids))


def _decode_json_value(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise AccountsContractError("Monday column value contains malformed JSON") from error