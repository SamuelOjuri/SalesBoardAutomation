"""Bind postcode evidence to the current project and one email/attachment source."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


_ASSET_START = re.compile(r"^EMAIL FILE ASSET \d+:$")
_ATTACHMENT_START = re.compile(
    r"^(?:(?:PDF|DOCX) ATTACHMENT|ATTACHMENT|INLINE IMAGE) \(.+\)(?::| \[)"
)
_HEADER = re.compile(r"^(from|to|cc|sent|date|subject):\s*(.*)$", re.I)
_QUOTE_START = re.compile(
    r"^(?:-{2,}\s*(?:original|forwarded)\s+message\s*-*|"
    r"begin forwarded message:|on .+wrote:)\s*$",
    re.I,
)
_GENERIC_PROJECT_WORDS = frozenset(
    "project name reference ref id school college court roof roofing plan drawing "
    "design insulation tapered revision request enquiry quote quotation delivery "
    "confirmation order site address location re fw fwd the a and for of".split()
)


class PostcodeEvidence(BaseModel):
    """Quotes are evidence only; none may supply publication control fields."""

    model_config = ConfigDict(extra="forbid")

    project_identity: str | None = Field(
        description="An explicit distinctive project name or stable project reference "
        "shared by the latest email and the postcode source. Use the same literal "
        "name/reference in both quotes; prefer a shared reference when names differ. "
        "Never use a generic word such as School, a company, or a postcode as the "
        "project identity. Null only when no project name/reference is identifiable."
    )
    current_project_quote: str | None = Field(
        max_length=1000,
        description="An exact contiguous quote from the top-level subject or latest "
        "unquoted message that contains project_identity. Null if identity is null."
    )
    source_project_quote: str | None = Field(
        max_length=1000,
        description="An exact contiguous quote containing the same project_identity "
        "from the subject/body of the message containing address_quote, or from that "
        "same attachment's project title/reference. Null if identity is null."
    )
    address_quote: str = Field(
        min_length=1, max_length=2000,
        description="An exact contiguous quote of the project/site/delivery address "
        "containing the selected postcode. For an area-only value include the "
        "structured Project field and its comma-suffixed area. Preserve line breaks. "
        "Do not combine separate messages, attachments, or different addresses."
    )


@dataclass(frozen=True)
class _Source:
    text: str
    current_text: str
    quoted: bool
    attachment: bool


def _lines(value: str) -> list[str]:
    # Outlook can use CRCRLF; quoted replies can add one or more > prefixes.
    return [
        re.sub(r"^\s*(?:>\s*)+", "", line).strip()
        for line in value.splitlines()
        if line.strip()
    ]


def _message_start(lines: list[str], index: int) -> bool:
    if _QUOTE_START.match(lines[index]):
        return True
    match = _HEADER.match(lines[index])
    if match is None or match.group(1).lower() != "from":
        return False
    headers: set[str] = set()
    for line in lines[index + 1 : index + 16]:
        match = _HEADER.match(line)
        if match is None:
            break
        headers.add(match.group(1).lower())
    return "subject" in headers and bool(headers & {"to", "sent", "date"})


def _sources(all_text: str) -> list[_Source]:
    assets: list[list[str]] = [[]]
    for line in _lines(all_text):
        if _ASSET_START.match(line):
            if assets[-1]:
                assets.append([])
        elif line != "EMAIL CONTENT:":
            assets[-1].append(line)

    result: list[_Source] = []
    for lines in assets:
        if not lines:
            continue
        chunks: list[tuple[list[str], bool, bool]] = []
        chunk: list[str] = []
        attachment = quoted = False
        for index, line in enumerate(lines):
            is_attachment = bool(_ATTACHMENT_START.match(line))
            is_message = not attachment and _message_start(lines, index)
            if is_attachment or (is_message and chunk):
                # A forwarded marker followed immediately by From is one boundary.
                if chunk and not all(_QUOTE_START.match(part) for part in chunk):
                    chunks.append((chunk, quoted, attachment))
                chunk = []
                attachment = is_attachment
                quoted = not is_attachment
            chunk.append(line)
        if chunk:
            chunks.append((chunk, quoted, attachment))
        current = "\n".join(chunks[0][0])
        result.extend(
            _Source("\n".join(chunk), current, quoted, attachment)
            for chunk, quoted, attachment in chunks
            if not (attachment and "[not processed:" in chunk[0])
        )
    return result


def _normalized(value: str) -> str:
    return " ".join(" ".join(_lines(value)).casefold().split())


def _contains_quote(text: str, quote: str | None) -> bool:
    normalized = _normalized(quote or "")
    if not normalized:
        return False
    prefix = r"(?<!\w)" if normalized[0].isalnum() else ""
    suffix = r"(?!\w)" if normalized[-1].isalnum() else ""
    return re.search(prefix + re.escape(normalized) + suffix, _normalized(text)) is not None


def _identity_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", value.casefold()))


def _contains_identity(text: str, identity: str) -> bool:
    tokens = _identity_tokens(identity)
    words = _identity_tokens(text)
    return bool(tokens) and any(
        words[i : i + len(tokens)] == tokens
        for i in range(len(words) - len(tokens) + 1)
    )


def _project_text(text: str) -> str:
    # Mailboxes, routing headers and URLs are not project identity evidence.
    lines = []
    for line in _lines(text):
        header = _HEADER.match(line)
        if header and header.group(1).lower() != "subject":
            continue
        if re.search(r"https?://|www\.|[\w.+-]+@[\w.-]+", line, re.I):
            continue
        lines.append(line)
    return "\n".join(lines)


def validated_project_context(all_text: str, evidence: PostcodeEvidence) -> str | None:
    """Return sources for the evidenced current project, or reject the association.

    This checks quote provenance and a literal shared identity. Semantic selection
    of the active project and identification of site addresses remain model tasks.
    """
    sources = _sources(all_text)
    candidates = [
        source for source in sources
        if _contains_quote(source.text, evidence.address_quote)
    ]
    identity = evidence.project_identity
    if identity is None:
        if (
            evidence.current_project_quote is not None
            or evidence.source_project_quote is not None
        ):
            return None
        # Preserve simple unnamed enquiries, but never borrow an unidentified
        # project from history or choose between multiple attachment sources.
        if any(source.quoted for source in sources):
            return None
        if len({source.current_text for source in sources}) != 1:
            return None
        if sum(source.attachment for source in sources) > 1:
            return None
        return "\n\n".join(source.text for source in sources) if candidates else None

    tokens = _identity_tokens(identity)
    if not any(
        len(token) >= 4 and token not in _GENERIC_PROJECT_WORDS for token in tokens
    ):
        return None
    if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", identity.strip(), re.I):
        return None
    if not _contains_identity(evidence.current_project_quote or "", identity):
        return None
    if not _contains_identity(evidence.source_project_quote or "", identity):
        return None

    for source in candidates:
        if not _contains_quote(
            _project_text(source.current_text), evidence.current_project_quote
        ):
            continue
        if not _contains_quote(
            _project_text(source.text), evidence.source_project_quote
        ):
            continue
        return "\n\n".join(
            part.text for part in sources
            if _contains_identity(_project_text(part.text), identity)
            and _contains_identity(_project_text(part.current_text), identity)
        )
    return None


def current_message_context(all_text: str) -> str:
    """An unevidenced fallback must not revive old projects or attachments."""
    return "\n\n".join(
        source.text for source in _sources(all_text)
        if not source.quoted and not source.attachment
    )
