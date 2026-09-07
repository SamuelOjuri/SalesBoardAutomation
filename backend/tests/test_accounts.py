from collections.abc import Mapping
from typing import Any

import pytest

from app.config import BOARD_CONTRACT
from app.behavioral_contract import AccountResolution
from app.services.accounts import (
    AccountRecord,
    AccountsContractError,
    AccountsIndex,
    AccountsIndexService,
    ContactRecord,
    ContactsIndex,
    ContactsIndexService,
    match_account,
    normalize_account_name,
    parse_account_item,
    parse_contact_item,
)
from app.services.requester_identity import RequesterIdentity, email_address_sha256


def _account(
    item_id: str,
    *,
    state: str = "active",
    domain: str | None = "example.co.uk",
    duplicate_values: object = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": f"Account {item_id}",
        "state": state,
        "board": {"id": str(BOARD_CONTRACT.accounts_board_id)},
        "column_values": [
            {
                "id": BOARD_CONTRACT.account_email_domain_column_id,
                "type": "text",
                "text": domain,
            },
            {
                "id": BOARD_CONTRACT.account_duplicate_column_id,
                "type": "dropdown",
                "values": duplicate_values,
            },
        ],
    }


class FakeAccountsClient:
    def __init__(
        self,
        pages: dict[str | None, Mapping[str, Any]],
        *,
        selected_item: Mapping[str, Any] | None = None,
    ) -> None:
        self.pages = pages
        self.selected_item = selected_item
        self.page_calls: list[str | None] = []
        self.item_calls: list[str] = []

    def load_accounts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        assert board_id == BOARD_CONTRACT.accounts_board_id
        assert limit == 500
        self.page_calls.append(cursor)
        return self.pages[cursor]

    def load_account_item(self, item_id: str) -> Mapping[str, Any] | None:
        self.item_calls.append(item_id)
        return self.selected_item


def _contact_item(
    item_id: str,
    *,
    email: str | None = "requester@example.co.uk",
    account_ids: tuple[str, ...] = ("10",),
    state: str = "active",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": f"Contact {item_id}",
        "state": state,
        "board": {"id": str(BOARD_CONTRACT.contacts_board_id)},
        "column_values": [
            {
                "id": BOARD_CONTRACT.contact_email_column_id,
                "type": "email",
                "email": email,
                "text": email,
                "value": (
                    None
                    if email is None
                    else f'{{"email":"{email}","text":"{email}"}}'
                ),
            },
            {
                "id": BOARD_CONTRACT.contact_accounts_relation_column_id,
                "type": "board_relation",
                "value": None,
                "linked_item_ids": list(account_ids),
            },
        ],
    }


class FakeContactsClient:
    def __init__(self, pages: dict[str | None, Mapping[str, Any]]) -> None:
        self.pages = pages
        self.page_calls: list[str | None] = []

    def load_contacts_page(
        self,
        board_id: int,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        assert board_id == BOARD_CONTRACT.contacts_board_id
        assert limit == 500
        self.page_calls.append(cursor)
        return self.pages[cursor]


def test_index_paginates_to_null_filters_only_duplicate_label_one_and_caches() -> None:
    client = FakeAccountsClient(
        {
            None: {
                "cursor": "next-page",
                "items": [
                    _account("1", duplicate_values=None),
                    _account("2", duplicate_values=[{"id": "1"}]),
                ],
            },
            "next-page": {
                "cursor": None,
                "items": [
                    _account("3", duplicate_values=[]),
                    _account("4", duplicate_values=[{"id": 2}]),
                    _account("5", state="archived", duplicate_values=[]),
                ],
            },
        }
    )
    now = [100.0]
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
        clock=lambda: now[0],
    )

    first = service.load_index()
    second = service.load_index()

    assert first is second
    assert client.page_calls == [None, "next-page"]
    assert [account.item_id for account in first.eligible_accounts] == ["1", "3", "4"]
    assert first.get("2").duplicate is True  # type: ignore[union-attr]
    assert first.get("4").duplicate is False  # type: ignore[union-attr]


def test_expired_index_fetches_every_page_again() -> None:
    client = FakeAccountsClient({None: {"cursor": None, "items": []}})
    now = [100.0]
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
        cache_ttl_seconds=300,
        clock=lambda: now[0],
    )

    service.load_index()
    now[0] = 401.0
    service.load_index()

    assert client.page_calls == [None, None]


def test_selected_account_is_refetched_and_must_still_be_eligible() -> None:
    client = FakeAccountsClient(
        {None: {"cursor": None, "items": [_account("42", duplicate_values=[])]}},
        selected_item=_account("42", duplicate_values=[{"id": 1}]),
    )
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
    )
    assert service.load_index().get("42") is not None

    assert service.revalidate_selected_account("42") is None
    assert client.item_calls == ["42"]


def test_selected_account_revalidation_rejects_an_item_from_another_board() -> None:
    selected_item = _account("42", duplicate_values=[])
    selected_item["board"] = {"id": "999"}
    client = FakeAccountsClient(
        {None: {"cursor": None, "items": []}},
        selected_item=selected_item,
    )
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
    )

    with pytest.raises(AccountsContractError, match="wrong board"):
        service.revalidate_selected_account("42")


@pytest.mark.parametrize(
    ("raw_value", "expected_ids"),
    [(None, ()), ('{"ids":[]}', ()), ('{"ids":[1]}', (1,))],
)
def test_legacy_duplicate_json_semantics_are_fail_closed(
    raw_value: str | None, expected_ids: tuple[int, ...]
) -> None:
    raw_item = _account("10")
    duplicate_column = raw_item["column_values"][1]
    duplicate_column.pop("values")
    duplicate_column["value"] = raw_value

    assert parse_account_item(raw_item).duplicate_label_ids == expected_ids


def test_malformed_duplicate_value_rejects_the_index() -> None:
    raw_item = _account("10", duplicate_values="selected")
    client = FakeAccountsClient(
        {None: {"cursor": None, "items": [raw_item]}}
    )
    service = AccountsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.accounts_board_id,
    )

    with pytest.raises(AccountsContractError, match="Duplicate values"):
        service.load_index()


def test_contacts_index_paginates_hashes_email_addresses_and_caches() -> None:
    client = FakeContactsClient(
        {
            None: {
                "cursor": "next-page",
                "items": [
                    _contact_item(
                        "100",
                        email="PERSON@Sales.Example.CO.UK",
                        account_ids=("10",),
                    )
                ],
            },
            "next-page": {
                "cursor": None,
                "items": [
                    _contact_item(
                        "200",
                        email="person@example.co.uk",
                        account_ids=("20",),
                        state="archived",
                    )
                ],
            },
        }
    )
    service = ContactsIndexService(
        client=client,
        board_id=BOARD_CONTRACT.contacts_board_id,
    )

    first = service.load_index()
    second = service.load_index()

    assert first is second
    assert client.page_calls == [None, "next-page"]
    assert first.contacts[0].email_address_sha256 == email_address_sha256(
        "person@sales.example.co.uk"
    )
    assert first.contacts[0].account_item_ids == ("10",)
    assert first.matching_email_sha256(
        email_address_sha256("person@example.co.uk") or ""
    ) == ()


def test_contact_parser_rejects_malformed_account_links() -> None:
    raw_item = _contact_item("100")
    raw_item["column_values"][1]["linked_item_ids"] = [True]

    with pytest.raises(AccountsContractError, match="link ID is malformed"):
        parse_contact_item(raw_item)


def _record(
    item_id: str,
    name: str,
    *,
    domain: str | None = None,
    active: bool = True,
    duplicate: bool = False,
) -> AccountRecord:
    return AccountRecord(
        item_id=item_id,
        name=name,
        active=active,
        email_domain=domain,
        duplicate_label_ids=(1,) if duplicate else (),
    )


def _contact(
    item_id: str,
    *,
    email_address: str = "requester@example.com",
    account_item_ids: tuple[str, ...] = ("10",),
    active: bool = True,
) -> ContactRecord:
    return ContactRecord(
        item_id=item_id,
        name=f"Contact {item_id}",
        active=active,
        email_address_sha256=email_address_sha256(email_address),
        account_item_ids=account_item_ids,
    )


def _requester(
    *,
    domain: str | None,
    company: str | None,
    website_domains: tuple[str, ...] = (),
    email_address: str = "requester@example.com",
) -> RequesterIdentity:
    return RequesterIdentity(
        email_address=email_address,
        domain=domain,
        company=company,
        source="top_level_sender",
        website_domains=website_domains,
    )


def test_unique_domain_match_requires_non_conflicting_name_evidence() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme Roofing Limited", domain="acme.co.uk"),
            _record("20", "Other Roofing Ltd", domain="other.co.uk"),
        )
    )

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company="Acme Roofing Ltd."),
    )

    assert result.resolution is AccountResolution.MATCHED
    assert result.account == index.get("10")
    assert result.reason == "unique_domain"
    assert result.domain_candidate_ids == ("10",)
    assert result.name_candidate_ids == ("10",)


def test_conflicting_domain_and_name_evidence_is_unresolved() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme Roofing", domain="acme.co.uk"),
            _record("20", "Other Roofing", domain="other.co.uk"),
        )
    )

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company="Other Roofing"),
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.reason == "not_found_or_ambiguous"
    assert result.domain_candidate_ids == ("10",)
    assert result.name_candidate_ids == ("20",)


def test_ambiguous_domain_match_is_unresolved() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme North", domain="acme.co.uk"),
            _record("20", "Acme South", domain="acme.co.uk"),
        )
    )

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company=None),
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.domain_candidate_ids == ("10", "20")


def test_shared_domain_is_disambiguated_by_unique_exact_name() -> None:
    index = AccountsIndex(
        (
            _record(
                "10",
                "M. K. M Building Supplies Limited",
                domain="mkm.com",
            ),
            _record(
                "20",
                "M. K. M Building Supplies (Holdings) Limited",
                domain="mkm.com",
            ),
        )
    )

    result = match_account(
        index,
        _requester(
            domain="mkm.com",
            company="M. K. M Building Supplies (Holdings) Ltd",
        ),
    )

    assert result.resolution is AccountResolution.MATCHED
    assert result.account == index.get("20")
    assert result.reason == "unique_domain_and_name"
    assert result.domain_candidate_ids == ("10", "20")
    assert result.name_candidate_ids == ("20",)


def test_shared_domain_remains_unresolved_when_name_matches_neither_candidate() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme North", domain="acme.co.uk"),
            _record("20", "Acme South", domain="acme.co.uk"),
            _record("30", "Other Roofing", domain="other.co.uk"),
        )
    )

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company="Other Roofing"),
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.domain_candidate_ids == ("10", "20")
    assert result.name_candidate_ids == ("30",)


def test_shared_domain_remains_unresolved_when_name_is_not_unique_within_domain() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme Limited", domain="acme.co.uk"),
            _record("20", "Acme PLC", domain="acme.co.uk"),
        )
    )

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company="Acme Ltd"),
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.domain_candidate_ids == ("10", "20")
    assert result.name_candidate_ids == ("10", "20")


def test_shared_domain_is_disambiguated_by_unique_contact_email() -> None:
    index = AccountsIndex(
        (
            _record(
                "1661824839",
                "Encon Insulation Limited",
                domain="encon.co.uk",
            ),
            _record("1661825541", "ECON Leeds", domain="encon.co.uk"),
            _record("1661825559", "ECON Plymouth", domain="encon.co.uk"),
        )
    )
    contacts = ContactsIndex(
        (
            _contact(
                "1662713491",
                email_address="s.morrissey@encon.co.uk",
                account_item_ids=("1661824839",),
            ),
        )
    )

    result = match_account(
        index,
        _requester(
            domain="encon.co.uk",
            company="Encon Group",
            email_address="S.Morrissey@ENCON.CO.UK",
        ),
        contacts=contacts,
    )

    assert result.resolution is AccountResolution.MATCHED
    assert result.account == index.get("1661824839")
    assert result.contact == contacts.contacts[0]
    assert result.reason == "unique_contact_email"
    assert result.domain_candidate_ids == (
        "1661824839",
        "1661825541",
        "1661825559",
    )
    assert result.name_candidate_ids == ()
    assert result.contact_candidate_ids == ("1662713491",)


def test_duplicate_contact_email_remains_unresolved() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme North", domain="acme.co.uk"),
            _record("20", "Acme South", domain="acme.co.uk"),
        )
    )
    contacts = ContactsIndex(
        (
            _contact("100", account_item_ids=("10",)),
            _contact("200", account_item_ids=("10",)),
        )
    )

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company=None),
        contacts=contacts,
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.contact is None
    assert result.contact_candidate_ids == ("100", "200")


def test_contact_linked_to_multiple_eligible_accounts_remains_unresolved() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme North", domain="acme.co.uk"),
            _record("20", "Acme South", domain="acme.co.uk"),
        )
    )
    contacts = ContactsIndex(
        (_contact("100", account_item_ids=("10", "20")),)
    )

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company=None),
        contacts=contacts,
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.contact_candidate_ids == ("100",)


def test_contact_email_cannot_override_conflicting_company_evidence() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme North", domain="acme.co.uk"),
            _record("20", "Acme South", domain="acme.co.uk"),
        )
    )
    contacts = ContactsIndex((_contact("100", account_item_ids=("10",)),))

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company="Acme South"),
        contacts=contacts,
    )

    assert result.resolution is AccountResolution.MATCHED
    assert result.account == index.get("20")
    assert result.reason == "unique_domain_and_name"


def test_unique_domain_match_does_not_require_company_evidence() -> None:
    index = AccountsIndex(
        (_record("10", "Acme Roofing", domain="acme.co.uk"),)
    )

    result = match_account(
        index,
        _requester(domain="acme.co.uk", company=None),
    )

    assert result.resolution is AccountResolution.MATCHED
    assert result.account == index.get("10")
    assert result.reason == "unique_domain"


def test_flagged_duplicate_is_filtered_before_name_uniqueness() -> None:
    index = AccountsIndex(
        (
            _record("1953164968", "Kingsgate Construction Ltd", duplicate=True),
            _record("1953164969", "Kingsgate Construction Limited"),
        )
    )

    result = match_account(
        index,
        _requester(domain=None, company="Kingsgate Construction"),
    )

    assert result.resolution is AccountResolution.MATCHED
    assert result.account == index.get("1953164969")
    assert result.reason == "unique_exact_name"
    assert result.name_candidate_ids == ("1953164969",)


def test_name_only_match_must_be_unique_and_eligible() -> None:
    index = AccountsIndex(
        (
            _record("10", "Acme Ltd"),
            _record("20", "Acme Limited", active=False),
            _record("30", "Acme PLC"),
        )
    )

    result = match_account(
        index,
        _requester(domain=None, company="Acme"),
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.name_candidate_ids == ("10", "30")


def test_name_fallback_for_unmatched_business_domain_requires_opt_in() -> None:
    index = AccountsIndex((_record("10", "Acme Roofing Ltd"),))
    requester = _requester(domain="acme.co.uk", company="Acme Roofing")

    blocked = match_account(index, requester)
    allowed = match_account(index, requester, allow_name_fallback=True)

    assert blocked.resolution is AccountResolution.UNRESOLVED
    assert blocked.account is None
    assert allowed.resolution is AccountResolution.MATCHED
    assert allowed.account == index.get("10")
    assert allowed.reason == "unique_exact_name"


def test_verified_account_domain_alias_requires_website_domain_agreement() -> None:
    index = AccountsIndex(
        (
            _record("1661824807", "AccuRoof", domain="accuroof.co.uk"),
            _record("20", "Other SIG Company", domain="other.example"),
        )
    )

    result = match_account(
        index,
        _requester(
            domain="sigplc.com",
            company="SIG PLC",
            website_domains=("accuroof.co.uk",),
        ),
        account_domain_aliases={"1661824807": ("sigplc.com",)},
    )

    assert result.resolution is AccountResolution.MATCHED
    assert result.account == index.get("1661824807")
    assert result.reason == "unique_domain_alias_website"
    assert result.domain_candidate_ids == ("1661824807",)
    assert result.name_candidate_ids == ()


def test_account_domain_alias_never_matches_without_website_agreement() -> None:
    index = AccountsIndex(
        (_record("1661824807", "AccuRoof", domain="accuroof.co.uk"),)
    )

    result = match_account(
        index,
        _requester(domain="sigplc.com", company="AccuRoof"),
        account_domain_aliases={"1661824807": ("sigplc.com",)},
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.domain_candidate_ids == ("1661824807",)
    assert result.name_candidate_ids == ("1661824807",)


def test_signature_website_never_matches_without_an_approved_requester_alias() -> None:
    index = AccountsIndex(
        (_record("1661824807", "AccuRoof", domain="accuroof.co.uk"),)
    )

    result = match_account(
        index,
        _requester(
            domain="sigplc.com",
            company="SIG PLC",
            website_domains=("accuroof.co.uk",),
        ),
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.domain_candidate_ids == ()


def test_account_domain_alias_rejects_conflicting_company_evidence() -> None:
    index = AccountsIndex(
        (
            _record("1661824807", "AccuRoof", domain="accuroof.co.uk"),
            _record("20", "Other SIG Company", domain="other.example"),
        )
    )

    result = match_account(
        index,
        _requester(
            domain="sigplc.com",
            company="Other SIG Company",
            website_domains=("accuroof.co.uk",),
        ),
        account_domain_aliases={"1661824807": ("sigplc.com",)},
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.domain_candidate_ids == ("1661824807",)
    assert result.name_candidate_ids == ("20",)


def test_similar_name_never_auto_links() -> None:
    index = AccountsIndex((_record("10", "Kingsgate Construction"),))

    result = match_account(
        index,
        _requester(domain=None, company="Kingsgates Construction"),
    )

    assert result.resolution is AccountResolution.UNRESOLVED
    assert result.account is None
    assert result.name_candidate_ids == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  ACME & Sons, LTD.  ", "acme and sons"),
        ("Acme-Sons Limited", "acme sons"),
        ("LLC", None),
        (None, None),
    ],
)
def test_account_name_normalization_is_exact_and_conservative(
    value: str | None,
    expected: str | None,
) -> None:
    assert normalize_account_name(value) == expected
