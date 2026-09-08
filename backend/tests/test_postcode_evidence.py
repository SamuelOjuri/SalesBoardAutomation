"""Synthetic regressions for reused email threads; no production mail is stored."""

import hashlib
from dataclasses import asdict
from datetime import datetime, timezone
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import BOARD_CONTRACT
from app.input_revision import EmailAssetIdentity
from app.services.intake import DownloadedEmailAsset
from app.services.postcode import (
    DesignParameterExtraction,
    GeminiPostcodeClient,
    analyze_downloaded_email_assets,
    extract_parameters,
)
from app.services.postcode_evidence import PostcodeEvidence


CURRENT_SUBJECT = "Subject: RE: Riverbank School - TP12776 - Delivery ETA"
OLD_SUBJECT = "Subject: RE: Hillcrest Grammar School - SL7 2BR"
CURRENT_ADDRESS = "Delivery address\nRiverbank Special School, Example Road, RG4 9RJ"
OLD_ADDRESS = "Delivery address\nHillcrest Grammar School, Example Lane, SL7 2BR"
THREAD = (
    "EMAIL FILE ASSET 1:\nEMAIL CONTENT:\n"
    "From: customer@example.com\nTo: sales@example.net\n"
    f"{CURRENT_SUBJECT}\nDate: 7 Sep 2026\nThanks.\n\n"
    "From: sales@example.net\nSent: 27 Aug 2026\nTo: customer@example.com\n"
    f"{CURRENT_SUBJECT}\n{CURRENT_ADDRESS}\n\n"
    "From: sales@example.net\nSent: 7 Jul 2026\nTo: customer@example.com\n"
    f"{OLD_SUBJECT}\n{OLD_ADDRESS}\nProject: Hillcrest Grammar School, SL"
)


def evidence(**changes: object) -> PostcodeEvidence:
    values = {
        "project_identity": "Riverbank School",
        "current_project_quote": CURRENT_SUBJECT,
        "source_project_quote": CURRENT_SUBJECT,
        "address_quote": CURRENT_ADDRESS,
    }
    values.update(changes)
    return PostcodeEvidence.model_validate(values)


def resolve(text: str, value: str | None, proof: PostcodeEvidence | None) -> str:
    return extract_parameters(text, extracted_parameters=DesignParameterExtraction(
        post_code=value, postcode_evidence=proof,
    ))["Post Code"]


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r\r\n", "\n\n"])
def test_older_same_project_address_remains_usable(line_ending: str) -> None:
    assert resolve(THREAD.replace("\n", line_ending), "RG4 9RJ", evidence()) == "RG"


@pytest.mark.parametrize("proof", [
    None,
    evidence(address_quote=OLD_ADDRESS),
    evidence(source_project_quote=OLD_SUBJECT, address_quote=OLD_ADDRESS),
    evidence(project_identity="Hillcrest Grammar School",
             current_project_quote=OLD_SUBJECT, source_project_quote=OLD_SUBJECT,
             address_quote=OLD_ADDRESS),
    evidence(project_identity=None, current_project_quote=None,
             source_project_quote=None, address_quote=OLD_ADDRESS),
])
def test_wrong_older_project_is_rejected_even_when_postcode_exists(proof) -> None:
    assert resolve(THREAD, "SL7 2BR", proof) == "Not provided"


def test_missing_model_result_cannot_revive_older_structured_project() -> None:
    assert resolve(THREAD, None, None) == "Not provided"


@pytest.mark.parametrize("quote", [
    "Riverbank School, Invented Road, RG4 9RJ",
    CURRENT_ADDRESS + "\n" + OLD_ADDRESS,
    "Delivery address\nHillcrest Grammar School, Example Lane, SL7 2BRX",
])
def test_fabricated_or_stitched_address_quotes_are_rejected(quote: str) -> None:
    assert resolve(THREAD, "RG4 9RJ", evidence(address_quote=quote)) == "Not provided"


def test_matching_area_alone_does_not_validate_a_different_full_postcode() -> None:
    assert resolve(THREAD, "RG1 1AA", evidence()) == "Not provided"


def test_area_only_value_requires_a_structured_project_suffix() -> None:
    assert resolve(THREAD, "RG", evidence()) == "Not provided"


def test_project_identity_uses_whole_tokens() -> None:
    proof = evidence(project_identity="River", current_project_quote=CURRENT_SUBJECT)
    assert resolve(THREAD, "RG4 9RJ", proof) == "Not provided"


@pytest.mark.parametrize("identity", ["School", "project", "27", "SL7 2BR"])
def test_generic_identity_or_postcode_is_not_a_project_match(identity: str) -> None:
    assert resolve(THREAD, "RG4 9RJ", evidence(project_identity=identity)) == "Not provided"


def test_quoted_reply_prefixes_and_gmail_boundary_are_supported() -> None:
    text = (
        f"{CURRENT_SUBJECT}\nThanks.\nOn Mon, 27 Aug, sender wrote:\n"
        f"> Project: Riverbank School\n> {CURRENT_ADDRESS.replace(chr(10), chr(10) + '> ')}\n"
        "On Mon, 7 Jul, sender wrote:\n> " + OLD_ADDRESS.replace("\n", "\n> ")
    )
    proof = evidence(source_project_quote="Project: Riverbank School")
    assert resolve(text, "RG4 9RJ", proof) == "RG"
    assert resolve(text, "SL7 2BR", proof.model_copy(update={"address_quote": OLD_ADDRESS})) == "Not provided"


@pytest.mark.parametrize("value", ["W", None])
def test_attachment_area_and_null_model_fallback_use_shared_project_reference(value) -> None:
    text = (
        "EMAIL CONTENT:\nSubject: 20426 - Lombrad Court - Revision\nPlease revise.\n"
        "PDF ATTACHMENT (plan.pdf):\nPROJECT: 20426 - LOMBARD COURT\nMain roof\n"
        "PDF ATTACHMENT (design.pdf):\nReference: 20426\n"
        "Project: Lombard Court, Acton, W\n"
        "PDF ATTACHMENT (old.pdf):\nProject: Hillcrest Grammar School, SL"
    )
    proof = evidence(
        project_identity="20426",
        current_project_quote="Subject: 20426 - Lombrad Court - Revision",
        source_project_quote="Reference: 20426",
        address_quote="Project: Lombard Court, Acton, W",
    )
    assert resolve(text, value, proof) == "W"


def test_cannot_borrow_a_project_quote_from_a_different_attachment() -> None:
    text = (
        f"{CURRENT_SUBJECT}\nPlease see plans.\n"
        "PDF ATTACHMENT (current.pdf):\nProject: Riverbank School\n"
        "PDF ATTACHMENT (old.pdf):\n" + OLD_ADDRESS
    )
    proof = evidence(source_project_quote="Project: Riverbank School", address_quote=OLD_ADDRESS)
    assert resolve(text, "SL7 2BR", proof) == "Not provided"


def test_cannot_borrow_current_project_from_a_different_email_asset() -> None:
    text = THREAD + (
        "\nEMAIL FILE ASSET 2:\nEMAIL CONTENT:\n"
        f"{OLD_SUBJECT}\n{OLD_ADDRESS}\n"
    )
    assert resolve(text, "SL7 2BR", evidence(address_quote=OLD_ADDRESS)) == "Not provided"


def test_latest_body_can_identify_project_when_subject_is_stale() -> None:
    text = f"{OLD_SUBJECT}\nPlease now quote for Riverbank School.\n{CURRENT_ADDRESS}"
    proof = evidence(
        current_project_quote="Please now quote for Riverbank School.",
        source_project_quote="Please now quote for Riverbank School.",
    )
    assert resolve(text, "RG4 9RJ", proof) == "RG"


def test_conflicting_structured_areas_for_same_project_still_fail_closed() -> None:
    text = (
        f"{CURRENT_SUBJECT}\nProject: Riverbank School, RG\n"
        "PDF ATTACHMENT (revision.pdf):\nProject: Riverbank School, SL"
    )
    proof = evidence(source_project_quote="Project: Riverbank School, RG",
                     address_quote="Project: Riverbank School, RG")
    assert resolve(text, "RG", proof) == "Not provided"


def test_simple_unnamed_email_keeps_normalization_and_inline_image_policy() -> None:
    text = (
        "Subject: Quotation\nProject location: WA4 6NL\n"
        "INLINE IMAGE (logo.png) [not processed: signature-safe policy]\n"
        "INLINE IMAGE (banner.png) [not processed: signature-safe policy]"
    )
    proof = evidence(project_identity=None, current_project_quote=None,
                     source_project_quote=None, address_quote="Project location: WA4 6NL")
    assert resolve(text, "Of Project Location: wa46nl", proof) == "WA"


def test_direct_structured_fallback_is_preserved_without_model_evidence() -> None:
    assert resolve("Project: Example College, LU", None, None) == "LU"


def test_full_postcode_quote_containing_conflicting_addresses_is_rejected() -> None:
    text = "Site: WA4 6NL or WA1 1AA"
    proof = evidence(project_identity=None, current_project_quote=None,
                     source_project_quote=None, address_quote=text)
    assert resolve(text, "WA4 6NL", proof) == "Not provided"


def test_address_quote_cannot_cut_a_postcode_out_of_a_longer_token() -> None:
    proof = evidence(project_identity=None, current_project_quote=None,
                     source_project_quote=None, address_quote="WA4 6NL")
    assert resolve("Reference: WA4 6NLX", "WA4 6NL", proof) == "Not provided"


def test_nested_evidence_cannot_supply_monday_control_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PostcodeEvidence.model_validate({**evidence().model_dump(), "board_id": "123"})


@pytest.mark.parametrize("correct", [True, False])
def test_mime_to_analysis_rejects_wrong_project_without_losing_company(tmp_path, correct) -> None:
    message = EmailMessage()
    message["From"] = "customer@example.com"
    message["To"] = "sales@example.net"
    message["Subject"] = CURRENT_SUBJECT.removeprefix("Subject: ")
    message.set_content("Company: Example Roofing\n" + THREAD.split("Thanks.", 1)[1])
    content = message.as_bytes()
    path = tmp_path / "enquiry.eml"
    path.write_bytes(content)
    asset = DownloadedEmailAsset(
        identity=EmailAssetIdentity(asset_id="1", filename=path.name,
                                    size_bytes=len(content), created_at=datetime.now(timezone.utc)),
        path=path, sha256=hashlib.sha256(content).hexdigest(),
    )
    proof = evidence() if correct else evidence(address_quote=OLD_ADDRESS)

    def generate_content(model, contents, config):
        assert "latest unquoted message" in contents
        assert "postcode_evidence" in config.response_json_schema["properties"]
        return SimpleNamespace(parsed={
            "post_code": "RG4 9RJ" if correct else "SL7 2BR",
            "company": "Example Roofing",
            "postcode_evidence": proof.model_dump(),
        })

    client = GeminiPostcodeClient(api_key="test", model="test", generate_content=generate_content)
    column = {
        "id": BOARD_CONTRACT.postcode_column_id, "type": "dropdown",
        "settings": {"labels": [{"id": 87, "label": "RG"}, {"id": 95, "label": "SL"}]},
    }
    result = analyze_downloaded_email_assets([asset], client=client, postcode_column=column)
    assert result.outcome == ("resolved" if correct else "not_found")
    assert result.monday_value == ({"ids": [87]} if correct else None)
    assert result.company == "Example Roofing"
    serialized = str(asdict(result))
    assert "address_quote" not in serialized
    assert "Riverbank" not in serialized
    assert "RG4 9RJ" not in serialized
