import json
from copy import deepcopy

import pytest

from app.config import BOARD_CONTRACT
from app.schema_validation import validate_sales_board_schema


@pytest.fixture
def valid_board() -> dict[str, object]:
    return {
        "id": str(BOARD_CONTRACT.sales_board_id),
        "columns": [
            {
                "id": BOARD_CONTRACT.email_file_column_id,
                "type": "file",
            },
            {
                "id": BOARD_CONTRACT.accounts_relation_column_id,
                "type": "board_relation",
                "settings": {"board_ids": [BOARD_CONTRACT.accounts_board_id]},
            },
            {
                "id": BOARD_CONTRACT.postcode_column_id,
                "type": "dropdown",
                "settings": {
                    "labels": [
                        {
                            "id": label.id,
                            "label": label.name,
                            "is_deactivated": False,
                        }
                        for label in BOARD_CONTRACT.required_postcode_labels
                    ]
                },
            },
        ],
    }


def issue_codes(board: dict[str, object]) -> set[str]:
    return {issue.code for issue in validate_sales_board_schema(board).issues}


def test_valid_schema_enables_publication(valid_board: dict[str, object]) -> None:
    result = validate_sales_board_schema(valid_board)

    assert result.publication_enabled
    assert result.issues == ()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda board: board.update(id="999"), "sales_board_id_mismatch"),
        (
            lambda board: board["columns"].pop(0),
            "required_column_missing",
        ),
        (
            lambda board: board["columns"][0].update(type="text"),
            "column_type_mismatch",
        ),
        (
            lambda board: board["columns"][1]["settings"].update(board_ids=[999]),
            "relation_target_mismatch",
        ),
        (
            lambda board: board["columns"][2]["settings"]["labels"][0].update(id=999),
            "postcode_label_mismatch",
        ),
    ],
)
def test_schema_drift_disables_publication(
    valid_board: dict[str, object],
    mutation: object,
    expected_code: str,
) -> None:
    board = deepcopy(valid_board)
    mutation(board)

    result = validate_sales_board_schema(board)

    assert not result.publication_enabled
    assert expected_code in issue_codes(board)


def test_legacy_settings_snapshot_is_supported(valid_board: dict[str, object]) -> None:
    board = deepcopy(valid_board)
    board["columns"][1].pop("settings")
    board["columns"][1]["settings_str"] = '{"boardIds":[1654217230]}'
    postcode_settings = board["columns"][2].pop("settings")
    for label in postcode_settings["labels"]:
        label["name"] = label.pop("label")
    board["columns"][2]["settings_str"] = json.dumps(postcode_settings)

    assert validate_sales_board_schema(board).publication_enabled
