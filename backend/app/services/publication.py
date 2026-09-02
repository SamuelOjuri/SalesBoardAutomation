"""Revalidated, fill-only publication of Sales item values to Monday."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import requests

from app.config import BOARD_CONTRACT
from app.input_revision import compute_input_revision
from app.monday_client import MondayTransientError
from app.publication_gate import PublicationGate
from app.services.accounts import AccountRecord
from app.services.intake import (
    IntakeContractError,
    is_excluded_sales_group,
    parse_sales_item_snapshot,
)


ColumnPublicationOutcome = Literal[
    "no_candidate",
    "already_set",
    "preserved_existing",
    "account_ineligible",
    "published",
]


class PublicationContractError(ValueError):
    """Raised when authoritative Monday publication data is unsafe."""


class StalePublicationError(PublicationContractError):
    """Raised when the Sales item no longer matches the analyzed input."""


class PublicationNotConfirmedError(RuntimeError):
    """Raised when a successful mutation is not visible in the post-read."""


class SalesPublicationClient(Protocol):
    def load_sales_item_for_publication(
        self, item_id: str
    ) -> Mapping[str, Any]: ...

    def change_sales_item_column_values(
        self,
        board_id: int,
        item_id: str,
        column_values: Mapping[str, object],
    ) -> None: ...


class AccountRevalidator(Protocol):
    def revalidate_selected_account(
        self, item_id: str
    ) -> AccountRecord | None: ...


@dataclass(frozen=True, slots=True)
class SalesPublicationSnapshot:
    item_id: str
    board_id: str
    group_id: str
    active: bool
    input_revision: str
    postcode_label_ids: tuple[str, ...]
    linked_account_item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ColumnPublicationResult:
    outcome: ColumnPublicationOutcome
    intended_id: str | None
    existing_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicationResult:
    item_id: str
    input_revision: str
    mutation_attempted: bool
    mutation_was_ambiguous: bool
    postcode: ColumnPublicationResult
    accounts: ColumnPublicationResult


def publish_sales_item(
    *,
    client: SalesPublicationClient,
    publication_gate: PublicationGate,
    item_id: str,
    input_revision: str,
    postcode_label_id: int | None = None,
    account_item_id: str | None = None,
    accounts: AccountRevalidator | None = None,
    excluded_group_ids: Sequence[str] = (),
) -> PublicationResult:
    normalized_item_id = _positive_decimal_id(item_id, "Sales item ID")
    normalized_revision = _input_revision(input_revision)
    normalized_postcode_id = _postcode_label_id(postcode_label_id)
    normalized_account_id = (
        _positive_decimal_id(account_item_id, "Account item ID")
        if account_item_id is not None
        else None
    )

    before = _load_current_snapshot(
        client,
        normalized_item_id,
        normalized_revision,
        excluded_group_ids=excluded_group_ids,
    )

    account_is_eligible = False
    if normalized_account_id is not None:
        if accounts is None:
            raise ValueError("accounts is required for an Account candidate")
        selected_account = accounts.revalidate_selected_account(
            normalized_account_id
        )
        account_is_eligible = (
            selected_account is not None
            and selected_account.item_id == normalized_account_id
            and selected_account.eligible
        )

    postcode = _decide_column(
        before.postcode_label_ids,
        normalized_postcode_id,
    )
    account = _decide_column(
        before.linked_account_item_ids,
        normalized_account_id,
        eligible=account_is_eligible,
        ineligible_outcome="account_ineligible",
    )

    column_values: dict[str, object] = {}
    if postcode is None:
        assert normalized_postcode_id is not None
        column_values[BOARD_CONTRACT.postcode_column_id] = {
            "ids": [int(normalized_postcode_id)]
        }
    if account is None:
        assert normalized_account_id is not None
        column_values[BOARD_CONTRACT.accounts_relation_column_id] = {
            "item_ids": [int(normalized_account_id)]
        }

    if not column_values:
        return PublicationResult(
            item_id=normalized_item_id,
            input_revision=normalized_revision,
            mutation_attempted=False,
            mutation_was_ambiguous=False,
            postcode=_require_decision(postcode),
            accounts=_require_decision(account),
        )

    publication_gate.require_publication_enabled()
    mutation_error: BaseException | None = None
    try:
        client.change_sales_item_column_values(
            BOARD_CONTRACT.sales_board_id,
            normalized_item_id,
            column_values,
        )
    except (
        requests.ConnectionError,
        requests.Timeout,
        MondayTransientError,
    ) as error:
        mutation_error = error

    after = _load_current_snapshot(
        client,
        normalized_item_id,
        normalized_revision,
        excluded_group_ids=excluded_group_ids,
    )
    postcode, postcode_confirmed = _reconcile_column(
        postcode,
        after.postcode_label_ids,
        normalized_postcode_id,
    )
    account, account_confirmed = _reconcile_column(
        account,
        after.linked_account_item_ids,
        normalized_account_id,
    )
    if not postcode_confirmed or not account_confirmed:
        if mutation_error is not None:
            raise mutation_error
        raise PublicationNotConfirmedError(
            "Monday mutation succeeded but the post-read did not confirm it"
        )

    return PublicationResult(
        item_id=normalized_item_id,
        input_revision=normalized_revision,
        mutation_attempted=True,
        mutation_was_ambiguous=mutation_error is not None,
        postcode=_require_decision(postcode),
        accounts=_require_decision(account),
    )


def parse_sales_publication_snapshot(
    raw_item: Mapping[str, Any],
    *,
    excluded_group_ids: Sequence[str] = (),
) -> SalesPublicationSnapshot:
    try:
        intake = parse_sales_item_snapshot(
            raw_item,
            contract=BOARD_CONTRACT,
            require_download_urls=False,
            excluded_group_ids=excluded_group_ids,
        )
        if is_excluded_sales_group(intake.group_id, excluded_group_ids):
            return SalesPublicationSnapshot(
                item_id=intake.item_id,
                board_id=intake.board_id,
                group_id=intake.group_id,
                active=intake.active,
                input_revision="",
                postcode_label_ids=(),
                linked_account_item_ids=(),
            )
        input_revision = compute_input_revision(
            asset.identity for asset in intake.email_assets
        )
    except (IntakeContractError, ValueError) as error:
        raise PublicationContractError(str(error)) from error

    columns = raw_item.get("column_values")
    if not isinstance(columns, list):
        raise PublicationContractError("Monday Sales item columns are missing")
    postcode_column = _typed_column(
        columns,
        BOARD_CONTRACT.postcode_column_id,
        "dropdown",
    )
    accounts_column = _typed_column(
        columns,
        BOARD_CONTRACT.accounts_relation_column_id,
        "board_relation",
    )
    return SalesPublicationSnapshot(
        item_id=intake.item_id,
        board_id=intake.board_id,
        group_id=intake.group_id,
        active=intake.active,
        input_revision=input_revision,
        postcode_label_ids=_typed_ids(postcode_column, "values", "id"),
        linked_account_item_ids=_typed_ids(
            accounts_column,
            "linked_item_ids",
            None,
        ),
    )


def _load_current_snapshot(
    client: SalesPublicationClient,
    item_id: str,
    input_revision: str,
    *,
    excluded_group_ids: Sequence[str] = (),
) -> SalesPublicationSnapshot:
    snapshot = parse_sales_publication_snapshot(
        client.load_sales_item_for_publication(item_id),
        excluded_group_ids=excluded_group_ids,
    )
    if snapshot.item_id != item_id:
        raise PublicationContractError("Monday returned the wrong Sales item")
    if snapshot.board_id != str(BOARD_CONTRACT.sales_board_id):
        raise PublicationContractError(
            "Monday returned the Sales item from the wrong board"
        )
    if not snapshot.active:
        raise StalePublicationError("Sales item is no longer active")
    if is_excluded_sales_group(snapshot.group_id, excluded_group_ids):
        raise StalePublicationError("Sales item belongs to an excluded group")
    if snapshot.input_revision != input_revision:
        raise StalePublicationError("Sales item input revision has changed")
    return snapshot


def _decide_column(
    existing_ids: tuple[str, ...],
    intended_id: str | None,
    *,
    eligible: bool = True,
    ineligible_outcome: ColumnPublicationOutcome = "no_candidate",
) -> ColumnPublicationResult | None:
    if intended_id is None:
        return ColumnPublicationResult("no_candidate", None, existing_ids)
    if intended_id in existing_ids:
        return ColumnPublicationResult(
            "already_set", intended_id, existing_ids
        )
    if existing_ids:
        return ColumnPublicationResult(
            "preserved_existing", intended_id, existing_ids
        )
    if not eligible:
        return ColumnPublicationResult(
            ineligible_outcome, intended_id, existing_ids
        )
    return None


def _reconcile_column(
    decision: ColumnPublicationResult | None,
    existing_ids: tuple[str, ...],
    intended_id: str | None,
) -> tuple[ColumnPublicationResult | None, bool]:
    if decision is not None:
        return decision, True
    assert intended_id is not None
    if intended_id in existing_ids:
        return (
            ColumnPublicationResult("published", intended_id, existing_ids),
            True,
        )
    if existing_ids:
        return (
            ColumnPublicationResult(
                "preserved_existing", intended_id, existing_ids
            ),
            True,
        )
    return None, False


def _require_decision(
    decision: ColumnPublicationResult | None,
) -> ColumnPublicationResult:
    if decision is None:
        raise PublicationNotConfirmedError("Monday publication was not confirmed")
    return decision


def _typed_column(
    columns: list[Any],
    column_id: str,
    expected_type: str,
) -> Mapping[str, Any]:
    matches = [
        column
        for column in columns
        if isinstance(column, Mapping) and column.get("id") == column_id
    ]
    if len(matches) != 1 or matches[0].get("type") != expected_type:
        raise PublicationContractError(
            f"Monday Sales item has an invalid {column_id} column"
        )
    return matches[0]


def _typed_ids(
    column: Mapping[str, Any],
    field: str,
    nested_field: str | None,
) -> tuple[str, ...]:
    if field not in column:
        raise PublicationContractError(
            f"Monday typed {column.get('id')} value is missing {field}"
        )
    values = column.get(field)
    if values is None:
        return ()
    if not isinstance(values, list):
        raise PublicationContractError(
            f"Monday typed {column.get('id')} value is malformed"
        )

    identifiers: list[str] = []
    for value in values:
        raw_identifier = value
        if nested_field is not None:
            if not isinstance(value, Mapping):
                raise PublicationContractError(
                    f"Monday typed {column.get('id')} value is malformed"
                )
            raw_identifier = value.get(nested_field)
        identifier = _positive_decimal_id(raw_identifier, "typed value ID")
        if identifier in identifiers:
            raise PublicationContractError(
                f"Monday typed {column.get('id')} contains duplicate IDs"
            )
        identifiers.append(identifier)
    return tuple(identifiers)


def _positive_decimal_id(value: object, field_name: str) -> str:
    if value is None or isinstance(value, bool):
        raise PublicationContractError(f"{field_name} is missing")
    normalized = str(value).strip()
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise PublicationContractError(f"{field_name} is malformed")
    return str(int(normalized))


def _input_revision(value: str) -> str:
    normalized = str(value).strip().casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise PublicationContractError("input revision must be a SHA-256")
    return normalized


def _postcode_label_id(value: int | None) -> str | None:
    if value is None:
        return None
    normalized = _positive_decimal_id(value, "Postcode label ID")
    allowed_ids = {
        str(label.id) for label in BOARD_CONTRACT.required_postcode_labels
    }
    if normalized not in allowed_ids:
        raise PublicationContractError(
            "Postcode label ID is outside the configured board contract"
        )
    return normalized
