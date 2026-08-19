"""Startup validation gate that prevents writes against schema drift."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.config import BOARD_CONTRACT, BoardContract
from app.schema_validation import (
    SchemaIssue,
    SchemaValidationResult,
    validate_sales_board_schema,
)


class PublicationDisabledError(RuntimeError):
    """Raised when a Monday mutation is attempted while publication is disabled."""


class PublicationGate:
    def __init__(self) -> None:
        self._result = SchemaValidationResult(
            issues=(
                SchemaIssue(
                    code="schema_not_validated",
                    message="The live Monday schema has not been validated.",
                ),
            )
        )

    @property
    def result(self) -> SchemaValidationResult:
        return self._result

    @property
    def publication_enabled(self) -> bool:
        return self._result.publication_enabled

    def apply_schema(
        self,
        board: Mapping[str, Any],
        contract: BoardContract = BOARD_CONTRACT,
    ) -> SchemaValidationResult:
        self._result = validate_sales_board_schema(board, contract)
        return self._result

    def disable(self, issue: SchemaIssue) -> None:
        self._result = SchemaValidationResult(issues=(issue,))

    def require_publication_enabled(self) -> None:
        if self.publication_enabled:
            return
        codes = ", ".join(issue.code for issue in self._result.issues)
        raise PublicationDisabledError(
            f"Monday publication is disabled by schema validation: {codes}."
        )


def validate_schema_at_startup(
    load_sales_board_schema: Callable[[int], Mapping[str, Any]],
    *,
    gate: PublicationGate | None = None,
    contract: BoardContract = BOARD_CONTRACT,
) -> PublicationGate:
    """Load the authoritative board schema and update the publication gate."""
    publication_gate = gate or PublicationGate()
    try:
        board = load_sales_board_schema(contract.sales_board_id)
    except Exception as error:
        publication_gate.disable(
            SchemaIssue(
                code="schema_fetch_failed",
                message=(
                    "Unable to load the live Monday schema "
                    f"({type(error).__name__})."
                ),
            )
        )
        return publication_gate

    publication_gate.apply_schema(board, contract)
    return publication_gate