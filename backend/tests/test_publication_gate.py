from collections.abc import Mapping
from typing import Any

import pytest

from app.config import BOARD_CONTRACT
from app.publication_gate import (
    PublicationDisabledError,
    PublicationGate,
    validate_schema_at_startup,
)


def valid_board() -> dict[str, Any]:
    return {
        "id": BOARD_CONTRACT.sales_board_id,
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
                        {"id": label.id, "label": label.name}
                        for label in BOARD_CONTRACT.required_postcode_labels
                    ]
                },
            },
        ],
    }


def test_gate_is_disabled_before_startup_validation() -> None:
    gate = PublicationGate()

    assert not gate.publication_enabled
    with pytest.raises(PublicationDisabledError, match="schema_not_validated"):
        gate.require_publication_enabled()


def test_valid_live_schema_enables_publication() -> None:
    requested_board_ids: list[int] = []

    def load_schema(board_id: int) -> Mapping[str, Any]:
        requested_board_ids.append(board_id)
        return valid_board()

    gate = validate_schema_at_startup(load_schema)

    assert gate.publication_enabled
    assert requested_board_ids == [BOARD_CONTRACT.sales_board_id]
    gate.require_publication_enabled()


def test_drifted_live_schema_keeps_publication_disabled() -> None:
    board = valid_board()
    board["columns"][1]["settings"]["board_ids"] = [999]

    gate = validate_schema_at_startup(lambda _: board)

    assert not gate.publication_enabled
    with pytest.raises(PublicationDisabledError, match="relation_target_mismatch"):
        gate.require_publication_enabled()


def test_schema_fetch_failure_keeps_publication_disabled() -> None:
    def fail_to_load(_: int) -> Mapping[str, Any]:
        raise TimeoutError("Monday did not respond")

    gate = validate_schema_at_startup(fail_to_load)

    assert not gate.publication_enabled
    assert gate.result.issues[0].code == "schema_fetch_failed"
    assert "Monday did not respond" not in gate.result.issues[0].message


def test_revalidation_can_disable_an_enabled_gate() -> None:
    gate = validate_schema_at_startup(lambda _: valid_board())
    drifted_board = valid_board()
    drifted_board["columns"][2]["settings"]["labels"].pop()

    gate.apply_schema(drifted_board)

    assert not gate.publication_enabled
    assert "postcode_label" in gate.result.issues[0].code
