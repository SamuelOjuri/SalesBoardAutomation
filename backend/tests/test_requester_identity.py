from app.services.email_parser import ParsedEmail
from app.services.requester_identity import (
    extract_requester_identity,
    normalize_domain,
)


def _parsed(from_value: str, body: str = "") -> ParsedEmail:
    return ParsedEmail(
        header=(
            f"From: {from_value}\n"
            "To: sales@taperedplus.co.uk\n"
            "Subject: Roof request\n"
            "Date: Wed, 19 Aug 2026 09:30:00 +0100"
        ),
        body=body,
        attachments=(),
    )


def test_external_top_level_sender_is_preferred_and_normalized() -> None:
    parsed = _parsed(
        "Requester <PERSON@Sales.WWW.Example.CO.UK>",
        body=(
            "-----Original Message-----\n"
            "From: Other Person <other@other.example>\n"
            "Sent: Wednesday, 19 August 2026 08:00\n"
            "To: sales@taperedplus.co.uk\n"
            "Subject: Older request"
        ),
    )

    identity = extract_requester_identity(
        parsed,
        internal_domains=["taperedplus.co.uk"],
        structured_company="  Example   Construction Ltd  ",
    )

    assert identity.email_address == "person@sales.www.example.co.uk"
    assert identity.domain == "example.co.uk"
    assert identity.company == "Example Construction Ltd"
    assert identity.source == "top_level_sender"


def test_internal_forward_uses_newest_external_forwarded_header() -> None:
    parsed = _parsed(
        "Sales <sales@taperedplus.co.uk>",
        body=(
            "-----Original Message-----\n"
            "From: Colleague <person@projects.taperedplus.co.uk>\n"
            "Sent: Wednesday, 19 August 2026 09:00\n"
            "To: sales@taperedplus.co.uk\n"
            "Subject: Fwd: Request\n\n"
            "-----Original Message-----\n"
            "From: Customer <CUSTOMER@www.B\u00dcCHER.DE>\n"
            "Sent: Wednesday, 19 August 2026 08:00\n"
            "To: person@taperedplus.co.uk\n"
            "Subject: Request"
        ),
    )

    identity = extract_requester_identity(
        parsed,
        internal_domains=["taperedplus.co.uk"],
    )

    assert identity.email_address == "customer@xn--bcher-kva.de"
    assert identity.domain == "xn--bcher-kva.de"
    assert identity.source == "forwarded_sender"


def test_generic_provider_is_not_automatic_domain_evidence() -> None:
    identity = extract_requester_identity(
        _parsed("Customer <Customer@GMAIL.COM>"),
        internal_domains=["taperedplus.co.uk"],
        structured_company="Kingsgate Construction",
    )

    assert identity.email_address == "customer@gmail.com"
    assert identity.domain is None
    assert identity.company == "Kingsgate Construction"
    assert identity.source == "top_level_sender"


def test_arbitrary_from_text_is_not_treated_as_a_forwarded_header() -> None:
    parsed = _parsed(
        "Sales <sales@taperedplus.co.uk>",
        body="Please collect the plans.\nFrom: attacker@example.com\nThank you.",
    )

    identity = extract_requester_identity(
        parsed,
        internal_domains=["taperedplus.co.uk"],
    )

    assert identity.email_address is None
    assert identity.domain is None
    assert identity.source == "not_found"


def test_domain_normalization_requires_a_public_suffix() -> None:
    assert normalize_domain(" WWW.Example.CO.UK. ") == "example.co.uk"
    assert normalize_domain("localhost") is None
    assert normalize_domain("not a domain") is None