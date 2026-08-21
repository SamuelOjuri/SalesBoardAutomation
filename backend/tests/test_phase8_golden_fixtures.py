import json
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from app.config import BOARD_CONTRACT
from app.services.accounts import AccountRecord, AccountsIndex, match_account
from app.services.email_parser import process_email_content
from app.services.postcode import (
    DesignParameterExtraction,
    extract_postcode_area,
    resolve_postcode_label,
)
from app.services.requester_identity import (
    extract_requester_identity,
    normalize_domain,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "phase8_golden_enquiries.json"
)
GOLDEN_FIXTURE: dict[str, Any] = json.loads(
    FIXTURE_PATH.read_text(encoding="utf-8")
)
GOLDEN_CASES: list[dict[str, Any]] = GOLDEN_FIXTURE["cases"]


def _email_bytes(case: dict[str, Any]) -> bytes:
    fixture_email = case["email"]
    message = EmailMessage()
    message["From"] = fixture_email["from"]
    message["To"] = fixture_email["to"]
    message["Subject"] = fixture_email["subject"]
    message.set_content(fixture_email["body"])
    return message.as_bytes()


def _accounts_index(case: dict[str, Any]) -> AccountsIndex:
    accounts = []
    for fixture_account in case["accounts"]:
        raw_domain = fixture_account["email_domain"]
        accounts.append(
            AccountRecord(
                item_id=fixture_account["item_id"],
                name=fixture_account["name"],
                active=fixture_account["active"],
                email_domain=(
                    normalize_domain(raw_domain)
                    if raw_domain is not None
                    else None
                ),
                duplicate_label_ids=tuple(
                    fixture_account["duplicate_label_ids"]
                ),
            )
        )
    return AccountsIndex(tuple(accounts))


def _postcode_column() -> dict[str, object]:
    return {
        "id": BOARD_CONTRACT.postcode_column_id,
        "type": "dropdown",
        "settings": {
            "labels": [
                {"id": label.id, "name": label.name}
                for label in BOARD_CONTRACT.required_postcode_labels
            ]
        },
    }


def test_golden_fixture_is_sanitized_and_manually_reviewed() -> None:
    raw_fixture = FIXTURE_PATH.read_text(encoding="utf-8")
    domains = {
        address.rpartition("@")[2].casefold()
        for address in re.findall(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+", raw_fixture, re.IGNORECASE
        )
    }

    assert GOLDEN_FIXTURE["schema_version"] == 1
    assert GOLDEN_FIXTURE["sanitized"] is True
    assert GOLDEN_FIXTURE["review_status"] == "manually_reviewed"
    assert domains <= {
        "gmail.com",
        "outlook.com",
        "taperedplus.co.uk",
        "northstar.example.com",
        "harbour.example.net",
        "east.example.org",
    }


@pytest.mark.parametrize(
    "case",
    GOLDEN_CASES,
    ids=[case["id"] for case in GOLDEN_CASES],
)
def test_reviewed_golden_enquiry_output(case: dict[str, Any]) -> None:
    parsed_email = process_email_content(_email_bytes(case), "enquiry.eml")
    extracted = DesignParameterExtraction.model_validate(case["model_output"])
    requester = extract_requester_identity(
        parsed_email,
        internal_domains=("taperedplus.co.uk",),
        structured_company=extracted.company,
    )
    postcode = resolve_postcode_label(
        extract_postcode_area(extracted.post_code),
        _postcode_column(),
    )
    account_match = match_account(_accounts_index(case), requester)
    expected = case["expected"]

    assert (postcode.area if postcode is not None else None) == expected[
        "postcode_area"
    ]
    assert (postcode.label_id if postcode is not None else None) == expected[
        "postcode_label_id"
    ]
    assert requester.domain == expected["requester_domain"]
    assert requester.source == expected["requester_source"]
    assert str(account_match.resolution) == expected["resolution"]
    assert (
        account_match.account.item_id
        if account_match.account is not None
        else None
    ) == expected["account_item_id"]
    assert account_match.reason == expected["reason"]
    assert list(account_match.domain_candidate_ids) == expected[
        "domain_candidate_ids"
    ]
    assert list(account_match.name_candidate_ids) == expected[
        "name_candidate_ids"
    ]