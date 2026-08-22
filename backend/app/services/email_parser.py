"""Deterministic EML/MSG parsing and supported attachment extraction."""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import PurePath
from typing import Any, Protocol
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import extract_msg


_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_DOCX_TEXT_PART_PATTERN = re.compile(
    r"^word/(?:document|header\d+|footer\d+|footnotes|endnotes)\.xml$"
)
_WORDPROCESSING_NAMESPACE = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
)
_WORD_PARAGRAPH_TAG = f"{{{_WORDPROCESSING_NAMESPACE}}}p"
_WORD_TEXT_TAG = f"{{{_WORDPROCESSING_NAMESPACE}}}t"
_WORD_TAB_TAG = f"{{{_WORDPROCESSING_NAMESPACE}}}tab"
_WORD_BREAK_TAGS = frozenset(
    {
        f"{{{_WORDPROCESSING_NAMESPACE}}}br",
        f"{{{_WORDPROCESSING_NAMESPACE}}}cr",
    }
)
_MAX_DOCX_MEMBERS = 2_048
_MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_MAX_DOCX_XML_PART_BYTES = 8 * 1024 * 1024


class AttachmentTextExtractor(Protocol):
    def process_pdf(self, content: bytes, filename: str) -> str: ...

    def process_image(
        self,
        content: bytes,
        filename: str,
        image_type: str = "ATTACHMENT",
    ) -> str: ...


class AttachmentExtractionError(RuntimeError):
    """Raised when a supported attachment cannot be safely converted to text."""


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    filename: str
    content: bytes
    content_type: str
    inline: bool = False


@dataclass(frozen=True, slots=True)
class ParsedEmail:
    header: str
    body: str
    attachments: tuple[EmailAttachment, ...]

    @property
    def email_text(self) -> str:
        return f"{self.header}\n{self.body}".strip()


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if tag.casefold() in {"script", "style"}:
            self._hidden_depth += 1
        elif tag.casefold() in {"br", "div", "p", "li", "tr"}:
            self._text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag.casefold() in {"div", "p", "li", "tr"}:
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self._text.append(data)

    def rendered_text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._text).splitlines())
        return "\n".join(line for line in lines if line)


def process_email_content(email_content: bytes, filename: str) -> ParsedEmail:
    suffix = PurePath(filename).suffix.casefold()
    if suffix == ".msg":
        return _parse_msg(email_content)
    if suffix == ".eml":
        return _parse_eml(email_content)
    raise ValueError(f"unsupported email extension {suffix!r}")


def extract_text_from_email(
    email_text: str,
    attachments: Sequence[EmailAttachment],
    *,
    extractor: AttachmentTextExtractor,
    include_inline_images: bool = False,
) -> str:
    sections = [f"EMAIL CONTENT:\n{email_text}"]
    for attachment in attachments:
        suffix = PurePath(attachment.filename).suffix.casefold()
        if (
            suffix in _IMAGE_EXTENSIONS
            and attachment.inline
            and not include_inline_images
        ):
            sections.append(
                f"INLINE IMAGE ({attachment.filename}) "
                "[not processed: signature-safe policy]"
            )
            continue
        if suffix not in {".pdf", ".docx", *_IMAGE_EXTENSIONS}:
            sections.append(
                f"ATTACHMENT ({attachment.filename}) [not processed: unsupported type]"
            )
            continue
        try:
            if suffix == ".pdf":
                text = extractor.process_pdf(
                    attachment.content, attachment.filename
                )
                sections.append(
                    f"PDF ATTACHMENT ({attachment.filename}):\n{text}"
                )
            elif suffix == ".docx":
                text = extract_docx_text(attachment.content)
                sections.append(
                    f"DOCX ATTACHMENT ({attachment.filename}):\n{text}"
                )
            else:
                image_type = "INLINE IMAGE" if attachment.inline else "ATTACHMENT"
                text = extractor.process_image(
                    attachment.content,
                    attachment.filename,
                    image_type,
                )
                sections.append(
                    f"{image_type} ({attachment.filename}):\n{text}"
                )
        except Exception as error:
            raise AttachmentExtractionError(
                f"supported {suffix} attachment text extraction failed"
            ) from error
    return "\n\n".join(sections)


def extract_docx_text(content: bytes) -> str:
    """Extract visible OOXML text without executing relationships or macros."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("DOCX attachment is not a valid ZIP archive") from error

    with archive:
        members = archive.infolist()
        if len(members) > _MAX_DOCX_MEMBERS:
            raise ValueError("DOCX attachment contains too many archive members")
        if any(member.flag_bits & 0x1 for member in members):
            raise ValueError("encrypted DOCX attachments are not supported")
        if sum(member.file_size for member in members) > _MAX_DOCX_UNCOMPRESSED_BYTES:
            raise ValueError("DOCX attachment expands beyond the safe size limit")

        names = {member.filename for member in members}
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise ValueError("DOCX attachment is missing required OOXML parts")

        text_parts: list[str] = []
        for member in members:
            if _DOCX_TEXT_PART_PATTERN.fullmatch(member.filename) is None:
                continue
            if member.file_size > _MAX_DOCX_XML_PART_BYTES:
                raise ValueError("DOCX XML part exceeds the safe size limit")
            raw_xml = archive.read(member)
            lowered = raw_xml.lower()
            if b"<!doctype" in lowered or b"<!entity" in lowered:
                raise ValueError("DOCX XML declarations are not supported")
            try:
                root = ElementTree.fromstring(raw_xml)
            except ElementTree.ParseError as error:
                raise ValueError("DOCX attachment contains malformed XML") from error
            part_text = _wordprocessing_text(root)
            if part_text:
                text_parts.append(part_text)

    return "\n".join(text_parts).strip() or "[no extractable text]"


def _wordprocessing_text(root: ElementTree.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in root.iter(_WORD_PARAGRAPH_TAG):
        fragments: list[str] = []
        for node in paragraph.iter():
            if node.tag == _WORD_TEXT_TAG:
                fragments.append(node.text or "")
            elif node.tag == _WORD_TAB_TAG:
                fragments.append("\t")
            elif node.tag in _WORD_BREAK_TAGS:
                fragments.append("\n")
        rendered = "".join(fragments)
        lines = [" ".join(line.split()) for line in rendered.splitlines()]
        normalized = "\n".join(line for line in lines if line)
        if normalized:
            paragraphs.append(normalized)
    return "\n".join(paragraphs)


def _parse_eml(content: bytes) -> ParsedEmail:
    message = BytesParser(policy=policy.default).parsebytes(content)
    header = _render_header(message)
    body = _preferred_body(message)
    attachments: list[EmailAttachment] = []
    for part in message.iter_attachments():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        attachments.append(
            EmailAttachment(
                filename=filename,
                content=payload,
                content_type=part.get_content_type(),
                inline=(
                    part.get_content_disposition() == "inline"
                    or bool(part.get("Content-ID"))
                ),
            )
        )
    return ParsedEmail(header, body, tuple(attachments))


def _parse_msg(content: bytes) -> ParsedEmail:
    with io.BytesIO(content) as email_buffer:
        message = extract_msg.Message(email_buffer)
        try:
            header = (
                f"From: {message.sender or ''}\n"
                f"To: {message.to or ''}\n"
                f"Subject: {message.subject or ''}\n"
                f"Date: {_format_email_date(message.date or '')}"
            )
            body = str(message.body or "").strip()
            if not body:
                raw_html = getattr(message, "htmlBody", None)
                if isinstance(raw_html, bytes):
                    body = _html_to_text(raw_html.decode("utf-8", errors="replace"))
                elif isinstance(raw_html, str):
                    body = _html_to_text(raw_html)

            attachments: list[EmailAttachment] = []
            for attachment in message.attachments:
                filename = attachment.longFilename or attachment.shortFilename
                payload = attachment.data
                if not filename or not isinstance(payload, bytes):
                    continue
                suffix = PurePath(filename).suffix.casefold()
                inline = suffix in _IMAGE_EXTENSIONS and bool(
                    getattr(attachment, "cid", None)
                )
                attachments.append(
                    EmailAttachment(
                        filename=filename,
                        content=payload,
                        content_type=_content_type_for_suffix(suffix),
                        inline=inline,
                    )
                )
            return ParsedEmail(header, body, tuple(attachments))
        finally:
            message.close()


def _render_header(message: Message) -> str:
    return (
        f"From: {message.get('from', '')}\n"
        f"To: {message.get('to', '')}\n"
        f"Subject: {message.get('subject', '')}\n"
        f"Date: {_format_email_date(message.get('date', ''))}"
    )


def _preferred_body(message: Message) -> str:
    if isinstance(message, EmailMessage):
        plain = message.get_body(preferencelist=("plain",))
        if plain is not None:
            return _part_text(plain).strip()
        html = message.get_body(preferencelist=("html",))
        if html is not None:
            return _html_to_text(_part_text(html))
    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    return str(payload or "").strip()


def _part_text(part: EmailMessage) -> str:
    value = part.get_content()
    if isinstance(value, bytes):
        return value.decode(part.get_content_charset() or "utf-8", errors="replace")
    return str(value)


def _html_to_text(value: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    return parser.rendered_text()


def _format_email_date(raw_date: str | datetime) -> str:
    if not raw_date:
        return ""
    try:
        parsed = (
            parsedate_to_datetime(raw_date)
            if isinstance(raw_date, str)
            else raw_date
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(ZoneInfo("Europe/London")).strftime(
            "%a, %d %b %Y %H:%M:%S %z"
        )
    except (TypeError, ValueError, OverflowError):
        return str(raw_date)


def _content_type_for_suffix(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".docx": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    }.get(suffix, "application/octet-stream")
