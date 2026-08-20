import hashlib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.input_revision import EmailAssetIdentity
from app.services.email_parser import process_email_content
from app.services.intake import DownloadedEmailAsset
from app.services.postcode import (
    DesignParameterExtraction,
    GeminiPostcodeClient,
    analyze_downloaded_email_assets,
)


def _postcode_column() -> dict[str, object]:
    return {
        "id": "dropdown_mm60y5x8",
        "type": "dropdown",
        "settings": {"labels": [{"id": 115, "name": "WA"}]},
    }


def _eml_bytes(
    plain_body: str | None,
    *,
    html_body: str | None = None,
    pdf_content: bytes | None = None,
    image_content: bytes | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "requester@example.com"
    message["To"] = "design@taperedplus.co.uk"
    message["Subject"] = "Roof request"
    message["Date"] = "Wed, 19 Aug 2026 09:30:00 +0100"
    if plain_body is not None:
        message.set_content(plain_body)
        if html_body is not None:
            message.add_alternative(html_body, subtype="html")
    else:
        message.set_content(html_body or "", subtype="html")
    if pdf_content is not None:
        message.add_attachment(
            pdf_content,
            maintype="application",
            subtype="pdf",
            filename="drawing.pdf",
        )
    if image_content is not None:
        message.add_attachment(
            image_content,
            maintype="image",
            subtype="png",
            filename="plan.png",
        )
    return message.as_bytes()


def _downloaded_asset(
    tmp_path: Path,
    *,
    asset_id: str,
    content: bytes,
) -> DownloadedEmailAsset:
    path = tmp_path / f"{asset_id}.eml"
    path.write_bytes(content)
    return DownloadedEmailAsset(
        identity=EmailAssetIdentity(
            asset_id=asset_id,
            filename=f"request-{asset_id}.eml",
            size_bytes=len(content),
            created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
        ),
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
    )


class FakeExtractionClient:
    def __init__(
        self, post_code: str | None, company: str | None = None
    ) -> None:
        self.post_code = post_code
        self.company = company
        self.context = ""
        self.pdf_filenames: list[str] = []

    def process_pdf(self, content: bytes, filename: str) -> str:
        self.pdf_filenames.append(filename)
        return content.decode("utf-8")

    def process_image(
        self,
        content: bytes,
        filename: str,
        image_type: str = "ATTACHMENT",
    ) -> str:
        del filename, image_type
        return content.decode("utf-8")

    def extract_design_parameters(self, context: str) -> DesignParameterExtraction:
        self.context = context
        return DesignParameterExtraction(
            post_code=self.post_code,
            company=self.company,
        )


def test_eml_uses_plain_text_before_html_fallback() -> None:
    parsed = process_email_content(
        _eml_bytes(
            "Plain project postcode WA4 6NL",
            html_body="<p>Wrong HTML postcode NE1 1AA</p>",
        ),
        "request.eml",
    )

    assert "WA4 6NL" in parsed.body
    assert "NE1 1AA" not in parsed.body

    html_only = process_email_content(
        _eml_bytes(None, html_body="<p>Project <strong>WA4 6NL</strong></p>"),
        "request.eml",
    )
    assert html_only.body == "Project WA4 6NL"


def test_msg_uses_html_fallback_and_closes_message(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAttachment:
        longFilename = "plan.png"
        shortFilename = None
        data = b"image postcode WA4 6NL"
        cid = "inline-plan"

    class FakeMessage:
        sender = "requester@example.com"
        to = "design@taperedplus.co.uk"
        subject = "Roof request"
        date = "Wed, 19 Aug 2026 09:30:00 +0100"
        body = ""
        htmlBody = b"<p>Project <strong>WA4 6NL</strong></p>"
        attachments = [FakeAttachment()]
        closed = False

        def close(self) -> None:
            self.closed = True

    fake_message = FakeMessage()
    monkeypatch.setattr(
        "app.services.email_parser.extract_msg.Message",
        lambda _buffer: fake_message,
    )

    parsed = process_email_content(b"fake-msg", "request.msg")

    assert parsed.body == "Project WA4 6NL"
    assert parsed.attachments[0].inline is True
    assert fake_message.closed is True


def test_assets_are_processed_in_numeric_id_order(tmp_path: Path) -> None:
    asset_10 = _downloaded_asset(
        tmp_path, asset_id="10", content=_eml_bytes("Second email")
    )
    asset_2 = _downloaded_asset(
        tmp_path, asset_id="2", content=_eml_bytes("First email")
    )
    client = FakeExtractionClient("WA4 6NL")

    result = analyze_downloaded_email_assets(
        [asset_10, asset_2],
        client=client,
        postcode_column=_postcode_column(),
    )

    assert result.asset_ids == ("2", "10")
    assert client.context.index("First email") < client.context.index("Second email")
    assert result.outcome == "resolved"
    assert result.monday_value == {"ids": [115]}


def test_project_postcode_can_come_only_from_pdf_attachment(tmp_path: Path) -> None:
    content = _eml_bytes(
        "Please see the attached drawing.",
        pdf_content=b"Drawing title block: WA4 6NL",
    )
    asset = _downloaded_asset(tmp_path, asset_id="7", content=content)
    client = FakeExtractionClient("WA4 6NL")

    result = analyze_downloaded_email_assets(
        [asset], client=client, postcode_column=_postcode_column()
    )

    assert client.pdf_filenames == ["drawing.pdf"]
    assert "Drawing title block: WA4 6NL" in client.context
    assert result.area == "WA"
    assert result.label_id == 115


def test_project_postcode_can_come_only_from_image_attachment(tmp_path: Path) -> None:
    content = _eml_bytes(
        "Please see the attached plan.",
        image_content=b"Plan title block: WA4 6NL",
    )
    asset = _downloaded_asset(tmp_path, asset_id="8", content=content)
    client = FakeExtractionClient("WA4 6NL")

    result = analyze_downloaded_email_assets(
        [asset], client=client, postcode_column=_postcode_column()
    )

    assert "Plan title block: WA4 6NL" in client.context
    assert result.area == "WA"
    assert result.label_id == 115


def test_structured_choice_can_ignore_other_addresses(tmp_path: Path) -> None:
    content = _eml_bytes(
        "Company office SW1A 1AA. Sender address M1 1AA. "
        "Project location postcode WA4 6NL."
    )
    asset = _downloaded_asset(tmp_path, asset_id="1", content=content)
    client = FakeExtractionClient("WA4 6NL")

    result = analyze_downloaded_email_assets(
        [asset], client=client, postcode_column=_postcode_column()
    )

    assert "SW1A 1AA" in client.context
    assert "M1 1AA" in client.context
    assert result.area == "WA"


def test_structured_company_is_carried_as_secondary_identity_evidence(
    tmp_path: Path,
) -> None:
    asset = _downloaded_asset(
        tmp_path,
        asset_id="1",
        content=_eml_bytes("Please quote for the supplied project."),
    )
    client = FakeExtractionClient(None, "  Kingsgate   Construction  ")

    result = analyze_downloaded_email_assets(
        [asset], client=client, postcode_column=_postcode_column()
    )

    assert result.company == "Kingsgate Construction"
    assert result.monday_value is None


def test_missing_postcode_is_unresolved_without_a_monday_value(tmp_path: Path) -> None:
    asset = _downloaded_asset(
        tmp_path, asset_id="1", content=_eml_bytes("No project address supplied")
    )

    result = analyze_downloaded_email_assets(
        [asset],
        client=FakeExtractionClient(None),
        postcode_column=_postcode_column(),
    )

    assert result.outcome == "not_found"
    assert result.area is None
    assert result.monday_value is None


def test_malformed_gemini_output_fails_strict_validation() -> None:
    class Response:
        parsed = None
        text = '{"post_code":"WA4 6NL","board_id":"5100711564"}'

    def generate_content(model: str, contents: Any, config: Any) -> Response:
        del model, contents, config
        return Response()

    client = GeminiPostcodeClient(
        api_key="test-key",
        model="test-model",
        generate_content=generate_content,
    )

    with pytest.raises(ValidationError, match="extra_forbidden"):
        client.extract_design_parameters("untrusted content")