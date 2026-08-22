"""Export the complete, paginated Accounts board as JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from app.config import get_settings
from app.monday_client import MondayClient
from app.services.accounts import (
    AccountRecord,
    AccountsIndex,
    AccountsIndexService,
    AccountsReader,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export every Accounts board item by following Monday pagination "
            "until the returned cursor is null."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON to this path instead of standard output.",
    )
    parser.add_argument(
        "--eligible-only",
        action="store_true",
        help="Export only active Accounts not marked with Duplicate label ID 1.",
    )
    return parser


def build_export_payload(
    index: AccountsIndex,
    *,
    board_id: int,
    eligible_only: bool = False,
) -> dict[str, Any]:
    accounts = index.eligible_accounts if eligible_only else index.accounts
    return {
        "accountsBoardId": str(board_id),
        "count": len(accounts),
        "eligibleOnly": eligible_only,
        "accounts": [_serialize_account(account) for account in accounts],
    }


def load_export_payload(
    *,
    client: AccountsReader,
    board_id: int,
    eligible_only: bool = False,
) -> dict[str, Any]:
    index = AccountsIndexService(
        client=client,
        board_id=board_id,
    ).load_index(force_refresh=True)
    return build_export_payload(
        index,
        board_id=board_id,
        eligible_only=eligible_only,
    )


def _serialize_account(account: AccountRecord) -> dict[str, Any]:
    return {
        "id": account.item_id,
        "name": account.name,
        "active": account.active,
        "emailDomain": account.email_domain,
        "duplicateLabelIds": list(account.duplicate_label_ids),
        "eligible": account.eligible,
    }


def main(argv: Sequence[str] | None = None) -> None:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    monday_client = MondayClient.from_settings(settings)
    try:
        payload = load_export_payload(
            client=monday_client,
            board_id=settings.accounts_board_id,
            eligible_only=arguments.eligible_only,
        )
    finally:
        monday_client.close()

    document = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if arguments.output is None:
        print(document, end="")
        return
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(document, encoding="utf-8")


if __name__ == "__main__":
    main()