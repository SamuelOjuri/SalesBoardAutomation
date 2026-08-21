"""Fail-closed validation of the live Monday Sales board schema."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from app.config import BOARD_CONTRACT, BoardContract


@dataclass(frozen=True)
class SchemaIssue:
    code: str
    message: str
    column_id: str | None = None


@dataclass(frozen=True)
class SchemaValidationResult:
    issues: tuple[SchemaIssue, ...]

    @property
    def publication_enabled(self) -> bool:
        return not self.issues


def validate_sales_board_schema(
    board: Mapping[str, Any],
    contract: BoardContract = BOARD_CONTRACT,
) -> SchemaValidationResult:
    """Validate a live board response and disable publication on any drift."""
    issues: list[SchemaIssue] = []

    if _normalise_id(board.get("id")) != str(contract.sales_board_id):
        issues.append(
            SchemaIssue(
                code="sales_board_id_mismatch",
                message=(
                    f"Expected Sales board {contract.sales_board_id}, "
                    f"received {board.get('id')!r}."
                ),
            )
        )

    raw_columns = board.get("columns")
    if not isinstance(raw_columns, list):
        issues.append(
            SchemaIssue(
                code="columns_missing",
                message="The live Sales board response did not contain a columns list.",
            )
        )
        return SchemaValidationResult(issues=tuple(issues))

    columns_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for raw_column in raw_columns:
        if not isinstance(raw_column, Mapping):
            continue
        column_id = _normalise_id(raw_column.get("id"))
        if column_id is not None:
            columns_by_id.setdefault(column_id, []).append(raw_column)

    _required_column(
        columns_by_id,
        contract.email_file_column_id,
        "file",
        issues,
    )
    relation_column = _required_column(
        columns_by_id,
        contract.accounts_relation_column_id,
        "board_relation",
        issues,
    )
    postcode_column = _required_column(
        columns_by_id,
        contract.postcode_column_id,
        "dropdown",
        issues,
    )

    if relation_column is not None:
        _validate_relation(relation_column, contract, issues)
    if postcode_column is not None:
        _validate_postcode_labels(postcode_column, contract, issues)

    return SchemaValidationResult(issues=tuple(issues))


def _required_column(
    columns_by_id: Mapping[str, list[Mapping[str, Any]]],
    column_id: str,
    expected_type: str,
    issues: list[SchemaIssue],
) -> Mapping[str, Any] | None:
    matches = columns_by_id.get(column_id, [])
    if not matches:
        issues.append(
            SchemaIssue(
                code="required_column_missing",
                message=f"Required column {column_id!r} is missing.",
                column_id=column_id,
            )
        )
        return None
    if len(matches) != 1:
        issues.append(
            SchemaIssue(
                code="duplicate_column_id",
                message=f"Column ID {column_id!r} appears more than once.",
                column_id=column_id,
            )
        )
        return None

    column = matches[0]
    if column.get("type") != expected_type:
        issues.append(
            SchemaIssue(
                code="column_type_mismatch",
                message=(
                    f"Column {column_id!r} must be type {expected_type!r}; "
                    f"received {column.get('type')!r}."
                ),
                column_id=column_id,
            )
        )
        return None
    return column


def _validate_settings(
    column: Mapping[str, Any],
    column_id: str,
    issues: list[SchemaIssue],
) -> Mapping[str, Any] | None:
    settings = column.get("settings")
    if isinstance(settings, Mapping):
        return settings

    legacy_settings = column.get("settings_str")
    if isinstance(legacy_settings, str):
        try:
            decoded = json.loads(legacy_settings)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, Mapping):
            return decoded

    issues.append(
        SchemaIssue(
            code="column_settings_invalid",
            message=f"Column {column_id!r} has missing or malformed settings.",
            column_id=column_id,
        )
    )
    return None


def _validate_relation(
    column: Mapping[str, Any],
    contract: BoardContract,
    issues: list[SchemaIssue],
) -> None:
    settings = _validate_settings(
        column,
        contract.accounts_relation_column_id,
        issues,
    )
    if settings is None:
        return

    raw_targets = settings.get("board_ids", settings.get("boardIds"))
    if not isinstance(raw_targets, list):
        raw_targets = [settings.get("board_id", settings.get("boardId"))]
    targets = {
        normalised
        for target in raw_targets
        if (normalised := _normalise_id(target)) is not None
    }
    expected_targets = {str(contract.accounts_board_id)}
    if targets != expected_targets:
        issues.append(
            SchemaIssue(
                code="relation_target_mismatch",
                message=(
                    f"Accounts relation must target only board "
                    f"{contract.accounts_board_id}; received {sorted(targets)!r}."
                ),
                column_id=contract.accounts_relation_column_id,
            )
        )


def _validate_postcode_labels(
    column: Mapping[str, Any],
    contract: BoardContract,
    issues: list[SchemaIssue],
) -> None:
    settings = _validate_settings(column, contract.postcode_column_id, issues)
    if settings is None:
        return

    raw_labels = settings.get("labels")
    if not isinstance(raw_labels, list):
        issues.append(
            SchemaIssue(
                code="postcode_labels_invalid",
                message="Postcode dropdown settings do not contain a labels list.",
                column_id=contract.postcode_column_id,
            )
        )
        return

    labels_by_name: dict[str, set[str]] = {}
    names_by_id: dict[str, set[str]] = {}
    for raw_label in raw_labels:
        if not isinstance(raw_label, Mapping):
            continue
        label_id = _normalise_id(raw_label.get("id"))
        # Monday's typed Column.settings payload uses ``label``. Retain
        # ``name`` only for the legacy settings snapshots supported above.
        label_name = raw_label.get("label", raw_label.get("name"))
        if label_id is None or not isinstance(label_name, str):
            continue
        labels_by_name.setdefault(label_name, set()).add(label_id)
        names_by_id.setdefault(label_id, set()).add(label_name)

    for required_label in contract.required_postcode_labels:
        expected_id = str(required_label.id)
        if labels_by_name.get(required_label.name) != {expected_id}:
            issues.append(
                SchemaIssue(
                    code="postcode_label_mismatch",
                    message=(
                        f"Postcode label {required_label.name!r} must have ID "
                        f"{required_label.id}."
                    ),
                    column_id=contract.postcode_column_id,
                )
            )
        if names_by_id.get(expected_id) != {required_label.name}:
            issues.append(
                SchemaIssue(
                    code="postcode_label_id_reused",
                    message=(
                        f"Postcode label ID {required_label.id} must identify only "
                        f"{required_label.name!r}."
                    ),
                    column_id=contract.postcode_column_id,
                )
            )


def _normalise_id(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, str)):
        return str(value)
    return None
