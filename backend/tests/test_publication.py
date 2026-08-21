import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import pytest

from app.config import BOARD_CONTRACT, DEFAULT_EXCLUDED_SALES_GROUP_IDS
from app.input_revision import EmailAssetIdentity, compute_input_revision
from app.monday_client import MondayTransientError
from app.publication_gate import PublicationDisabledError, PublicationGate
from app.services.accounts import AccountRecord
from app.services.publication import (
    PublicationContractError,
    StalePublicationError,
    parse_sales_publication_snapshot,
    publish_sales_item,
)


ASSET = EmailAssetIdentity(
    asset_id="10",
    filename="request.eml",
    size_bytes=20,
    created_at=datetime(2026, 8, 21, 10, tzinfo=timezone.utc),
)
INPUT_REVISION = compute_input_revision([ASSET])


def _sales_item(
    *,
    postcode_ids: tuple[int, ...] = (),
    account_ids: tuple[str, ...] = (),
    asset: EmailAssetIdentity = ASSET,
    group_id: str = "topics",
) -> dict[str, Any]:
    return {
        "id": "42",
        "state": "active",
        "board": {"id": str(BOARD_CONTRACT.sales_board_id)},
        "group": {"id": group_id, "title": "Test Group"},
        "assets": [
            {
                "id": asset.asset_id,
                "name": asset.filename,
                "file_size": asset.size_bytes,
                "created_at": asset.created_at.isoformat(),
                "url": "https://files.monday.com/request.eml",
                "public_url": None,
            }
        ],
        "column_values": [
            {
                "id": BOARD_CONTRACT.email_file_column_id,
                "type": "file",
                "value": json.dumps(
                    {"files": [{"assetId": asset.asset_id}]}
                ),
            },
            {
                "id": BOARD_CONTRACT.postcode_column_id,
                "type": "dropdown",
                "value": None,
                "values": [
                    {"id": label_id, "label": "label"}
                    for label_id in postcode_ids
                ],
            },
            {
                "id": BOARD_CONTRACT.accounts_relation_column_id,
                "type": "board_relation",
                "value": None,
                "linked_item_ids": list(account_ids),
            },
        ],
    }


class FakePublicationClient:
    def __init__(
        self,
        snapshots: list[Mapping[str, Any]],
        *,
        mutation_error: BaseException | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.mutation_error = mutation_error
        self.reads: list[str] = []
        self.mutations: list[tuple[int, str, Mapping[str, object]]] = []

    def load_sales_item_for_publication(
        self, item_id: str
    ) -> Mapping[str, Any]:
        self.reads.append(item_id)
        return self.snapshots.pop(0)

    def change_sales_item_column_values(
        self,
        board_id: int,
        item_id: str,
        column_values: Mapping[str, object],
    ) -> None:
        self.mutations.append((board_id, item_id, column_values))
        if self.mutation_error is not None:
            raise self.mutation_error


class FakeAccounts:
    def __init__(self, selected: AccountRecord | None) -> None:
        self.selected = selected
        self.calls: list[str] = []

    def revalidate_selected_account(
        self, item_id: str
    ) -> AccountRecord | None:
        self.calls.append(item_id)
        return self.selected


def _account(item_id: str = "1953164969") -> AccountRecord:
    return AccountRecord(
        item_id=item_id,
        name="Kingsgate Construction",
        active=True,
        email_domain="kingsgate.co.uk",
        duplicate_label_ids=(),
    )


def _enabled_gate() -> PublicationGate:
    gate = PublicationGate()
    gate.apply_schema(
        {
            "id": BOARD_CONTRACT.sales_board_id,
            "columns": [
                {"id": BOARD_CONTRACT.email_file_column_id, "type": "file"},
                {
                    "id": BOARD_CONTRACT.accounts_relation_column_id,
                    "type": "board_relation",
                    "settings": {
                        "board_ids": [BOARD_CONTRACT.accounts_board_id]
                    },
                },
                {
                    "id": BOARD_CONTRACT.postcode_column_id,
                    "type": "dropdown",
                    "settings": {
                        "labels": [
                            {"id": label.id, "label": label.name}
                            for label in BOARD_CONTRACT.required_postcode_labels
                        ]
                    },
                },
            ],
        }
    )
    return gate


def test_publishes_only_empty_columns_and_confirms_with_post_read() -> None:
    client = FakePublicationClient(
        [
            _sales_item(),
            _sales_item(
                postcode_ids=(115,),
                account_ids=("1953164969",),
            ),
        ]
    )
    accounts = FakeAccounts(_account())

    result = publish_sales_item(
        client=client,
        publication_gate=_enabled_gate(),
        item_id="42",
        input_revision=INPUT_REVISION,
        postcode_label_id=115,
        account_item_id="1953164969",
        accounts=accounts,
    )

    assert result.mutation_attempted is True
    assert result.mutation_was_ambiguous is False
    assert result.postcode.outcome == "published"
    assert result.accounts.outcome == "published"
    assert client.reads == ["42", "42"]
    assert accounts.calls == ["1953164969"]
    assert client.mutations == [
        (
            BOARD_CONTRACT.sales_board_id,
            "42",
            {
                BOARD_CONTRACT.postcode_column_id: {"ids": [115]},
                BOARD_CONTRACT.accounts_relation_column_id: {
                    "item_ids": [1953164969]
                },
            },
        )
    ]


def test_existing_values_are_reported_and_never_overwritten() -> None:
    client = FakePublicationClient(
        [_sales_item(postcode_ids=(115,), account_ids=("99",))]
    )

    result = publish_sales_item(
        client=client,
        publication_gate=_enabled_gate(),
        item_id="42",
        input_revision=INPUT_REVISION,
        postcode_label_id=115,
        account_item_id="1953164969",
        accounts=FakeAccounts(_account()),
    )

    assert result.mutation_attempted is False
    assert result.postcode.outcome == "already_set"
    assert result.accounts.outcome == "preserved_existing"
    assert result.accounts.existing_ids == ("99",)
    assert client.mutations == []


def test_missing_postcode_never_clears_an_existing_value() -> None:
    client = FakePublicationClient([_sales_item(postcode_ids=(115,))])

    result = publish_sales_item(
        client=client,
        publication_gate=_enabled_gate(),
        item_id="42",
        input_revision=INPUT_REVISION,
        postcode_label_id=None,
    )

    assert result.mutation_attempted is False
    assert result.postcode.outcome == "no_candidate"
    assert result.postcode.existing_ids == ("115",)
    assert client.mutations == []


def test_ineligible_account_does_not_block_postcode_publication() -> None:
    client = FakePublicationClient(
        [_sales_item(), _sales_item(postcode_ids=(115,))]
    )

    result = publish_sales_item(
        client=client,
        publication_gate=_enabled_gate(),
        item_id="42",
        input_revision=INPUT_REVISION,
        postcode_label_id=115,
        account_item_id="1953164969",
        accounts=FakeAccounts(None),
    )

    assert result.postcode.outcome == "published"
    assert result.accounts.outcome == "account_ineligible"
    assert client.mutations[0][2] == {
        BOARD_CONTRACT.postcode_column_id: {"ids": [115]}
    }


def test_changed_input_revision_blocks_all_writes() -> None:
    changed_asset = EmailAssetIdentity(
        asset_id="11",
        filename="new.eml",
        size_bytes=21,
        created_at=ASSET.created_at,
    )
    client = FakePublicationClient([_sales_item(asset=changed_asset)])

    with pytest.raises(StalePublicationError, match="revision has changed"):
        publish_sales_item(
            client=client,
            publication_gate=_enabled_gate(),
            item_id="42",
            input_revision=INPUT_REVISION,
            postcode_label_id=115,
        )

    assert client.mutations == []


def test_excluded_group_blocks_all_writes() -> None:
    excluded_group_id = DEFAULT_EXCLUDED_SALES_GROUP_IDS[0]
    client = FakePublicationClient(
        [_sales_item(group_id=excluded_group_id)]
    )

    with pytest.raises(StalePublicationError, match="excluded group"):
        publish_sales_item(
            client=client,
            publication_gate=_enabled_gate(),
            item_id="42",
            input_revision=INPUT_REVISION,
            postcode_label_id=115,
            excluded_group_ids=(excluded_group_id,),
        )

    assert client.reads == ["42"]
    assert client.mutations == []


def test_disabled_schema_gate_blocks_the_mutation() -> None:
    client = FakePublicationClient([_sales_item()])

    with pytest.raises(PublicationDisabledError):
        publish_sales_item(
            client=client,
            publication_gate=PublicationGate(),
            item_id="42",
            input_revision=INPUT_REVISION,
            postcode_label_id=115,
        )

    assert client.mutations == []


def test_ambiguous_mutation_is_accepted_only_when_post_read_confirms_it() -> None:
    client = FakePublicationClient(
        [_sales_item(), _sales_item(postcode_ids=(115,))],
        mutation_error=MondayTransientError("gateway timeout"),
    )

    result = publish_sales_item(
        client=client,
        publication_gate=_enabled_gate(),
        item_id="42",
        input_revision=INPUT_REVISION,
        postcode_label_id=115,
    )

    assert result.mutation_was_ambiguous is True
    assert result.postcode.outcome == "published"
    assert client.reads == ["42", "42"]


def test_unconfirmed_ambiguous_mutation_is_raised_only_after_post_read() -> None:
    client = FakePublicationClient(
        [_sales_item(), _sales_item()],
        mutation_error=MondayTransientError("gateway timeout"),
    )

    with pytest.raises(MondayTransientError, match="gateway timeout"):
        publish_sales_item(
            client=client,
            publication_gate=_enabled_gate(),
            item_id="42",
            input_revision=INPUT_REVISION,
            postcode_label_id=115,
        )

    assert client.reads == ["42", "42"]
    assert len(client.mutations) == 1


def test_typed_publication_values_are_required() -> None:
    raw_item = _sales_item()
    postcode_column = raw_item["column_values"][1]
    postcode_column.pop("values")

    with pytest.raises(PublicationContractError, match="missing values"):
        parse_sales_publication_snapshot(raw_item)
