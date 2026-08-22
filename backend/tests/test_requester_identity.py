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
    assert identity.website_domains == ()


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
            "Subject: Request\n\n"
            "Website: https://www.customer.de"
        ),
    )

    identity = extract_requester_identity(
        parsed,
        internal_domains=["taperedplus.co.uk"],
    )

    assert identity.email_address == "customer@xn--bcher-kva.de"
    assert identity.domain == "xn--bcher-kva.de"
    assert identity.source == "forwarded_sender"
    assert identity.website_domains == ("customer.de",)


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


def test_exact_relay_host_can_map_to_the_verified_business_domain() -> None:
    identity = extract_requester_identity(
        _parsed("Tremco Support <support@tremcocpgsupport.zendesk.com>"),
        internal_domains=["taperedplus.co.uk"],
        domain_aliases={
            "tremcocpgsupport.zendesk.com": "tremcocpg.com",
        },
    )

    assert identity.email_address == "support@tremcocpgsupport.zendesk.com"
    assert identity.domain == "tremcocpg.com"
    assert identity.source == "top_level_sender"


def test_signature_website_unwraps_verified_cudasvc_destination() -> None:
    identity = extract_requester_identity(
        _parsed(
            '"Dunsmore, Jamie" <jamiedunsmore@sigplc.com>',
            body=(
                "Best Regards\n"
                "Jamie Dunsmore\n"
                "W: https://linkprotect.cudasvc.com/url?"
                "a=https%3A%2F%2Fwww.accuroof.co.uk%2Fcontact&c=opaque\n"
                "Terms: https://www.sigplc.com/terms"
            ),
        ),
        internal_domains=["taperedplus.co.uk"],
    )

    assert identity.domain == "sigplc.com"
    assert identity.website_domains == ("accuroof.co.uk",)


def test_signature_website_ignores_wrappers_without_one_destination() -> None:
    identity = extract_requester_identity(
        _parsed(
            "Requester <requester@example.com>",
            body="W: https://linkprotect.cudasvc.com/url?c=opaque",
        ),
        internal_domains=["taperedplus.co.uk"],
    )

    assert identity.website_domains == ()


def test_signature_website_supports_direct_and_safe_links_destinations() -> None:
    identity = extract_requester_identity(
        _parsed(
            "Requester <requester@example.com>",
            body=(
                "Web: www.Example.CO.UK/contact\n"
                "Website: https://eur03.safelinks.protection.outlook.com/?"
                "url=https%3A%2F%2Fcustomer.example.net%2Fhome&data=opaque"
            ),
        ),
        internal_domains=["taperedplus.co.uk"],
    )

    assert identity.website_domains == ("example.co.uk", "example.net")


def test_top_level_sender_does_not_inherit_quoted_signature_website() -> None:
    identity = extract_requester_identity(
        _parsed(
            "Current Sender <current@example.com>",
            body=(
                "Please see below.\n\n"
                "-----Original Message-----\n"
                "From: Older Sender <older@other.example.com>\n"
                "Sent: Tuesday, 18 August 2026 09:00\n"
                "To: current@example.com\n"
                "Subject: Older request\n\n"
                "W: https://other.example.com"
            ),
        ),
        internal_domains=["taperedplus.co.uk"],
    )

    assert identity.website_domains == ()


def test_relay_alias_does_not_apply_to_unlisted_subdomains() -> None:
    identity = extract_requester_identity(
        _parsed("Other Tenant <support@othercompany.zendesk.com>"),
        internal_domains=["taperedplus.co.uk"],
        domain_aliases={"zendesk.com": "tremcocpg.com"},
    )

    assert identity.domain == "zendesk.com"


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
