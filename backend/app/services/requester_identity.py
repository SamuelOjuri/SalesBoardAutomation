"""Deterministic requester identity extraction from trusted email structure."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from email import policy
from email.parser import Parser
from email.utils import getaddresses
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import tldextract

from app.services.email_parser import ParsedEmail


_GENERIC_EMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "mac.com",
        "me.com",
        "msn.com",
        "outlook.com",
        "proton.me",
        "protonmail.com",
        "yahoo.com",
        "yahoo.co.uk",
    }
)
_FORWARDED_MARKER_PATTERN = re.compile(
    r"^\s*(?:-{2,}\s*(?:original|forwarded)\s+message\s*-*|"
    r"begin\s+forwarded\s+message:)\s*$",
    re.IGNORECASE,
)
_HEADER_LINE_PATTERN = re.compile(
    r"^\s*(from|sent|date|to|cc|subject):\s*(.*)$",
    re.IGNORECASE,
)
_SIGNATURE_WEBSITE_LINE_PATTERN = re.compile(
    r"^\s*(?:w|web|website)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_WEBSITE_CANDIDATE_PATTERN = re.compile(
    r"(?:https?://|www\.)[^\s<>()]+|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?:/[^\s<>()]*)?",
    re.IGNORECASE,
)
_MAX_SIGNATURE_WEBSITE_LINE_LENGTH = 8_192
_MAX_WRAPPED_URL_DEPTH = 2
_DOMAIN_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())


RequesterSource = Literal["top_level_sender", "forwarded_sender", "not_found"]


@dataclass(frozen=True, slots=True)
class RequesterIdentity:
    email_address: str | None
    domain: str | None
    company: str | None
    source: RequesterSource
    website_domains: tuple[str, ...] = ()


def extract_requester_identity(
    parsed_email: ParsedEmail,
    *,
    internal_domains: list[str] | tuple[str, ...],
    structured_company: str | None = None,
    domain_aliases: Mapping[str, str] | None = None,
) -> RequesterIdentity:
    """Select the first trustworthy external sender and its domain evidence."""

    normalized_internal_domains = frozenset(
        domain
        for value in internal_domains
        if (domain := normalize_domain(value)) is not None
    )
    normalized_domain_aliases = _normalize_domain_aliases(domain_aliases or {})
    company = normalize_company(structured_company)

    top_level_sender = _top_level_sender(
        parsed_email.header,
        domain_aliases=normalized_domain_aliases,
    )
    if top_level_sender is not None and not _is_internal(
        top_level_sender[1], normalized_internal_domains
    ):
        return _identity(
            top_level_sender,
            company,
            "top_level_sender",
            website_domains=_signature_website_domains(
                _top_level_message_body(parsed_email.body)
            ),
        )

    for sender, sender_body in _forwarded_sender_sections(
        parsed_email.body,
        domain_aliases=normalized_domain_aliases,
    ):
        if not _is_internal(sender[1], normalized_internal_domains):
            return _identity(
                sender,
                company,
                "forwarded_sender",
                website_domains=_signature_website_domains(sender_body),
            )

    return RequesterIdentity(
        email_address=None,
        domain=None,
        company=company,
        source="not_found",
        website_domains=(),
    )


def normalize_domain(value: object) -> str | None:
    ascii_domain = _normalize_host_domain(value)
    if ascii_domain is None:
        return None
    extracted = _DOMAIN_EXTRACTOR(ascii_domain)
    registrable = extracted.top_domain_under_public_suffix
    return registrable.casefold() or None


def _normalize_host_domain(value: object) -> str | None:
    candidate = str(value).strip().casefold().rstrip(".")
    if candidate.startswith("www."):
        candidate = candidate[4:]
    if not candidate or "@" in candidate or any(
        character.isspace() for character in candidate
    ):
        return None
    try:
        ascii_domain = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return ascii_domain


def _identity(
    sender: tuple[str, str],
    company: str | None,
    source: RequesterSource,
    *,
    website_domains: tuple[str, ...],
) -> RequesterIdentity:
    email_address, sender_domain = sender
    domain = None if sender_domain in _GENERIC_EMAIL_DOMAINS else sender_domain
    return RequesterIdentity(
        email_address=email_address,
        domain=domain,
        company=company,
        source=source,
        website_domains=website_domains,
    )


def _top_level_sender(
    header: str,
    *,
    domain_aliases: Mapping[str, str],
) -> tuple[str, str] | None:
    message = Parser(policy=policy.default).parsestr(f"{header}\n\n")
    return _first_mailbox(
        message.get_all("from", []),
        domain_aliases=domain_aliases,
    )


def _forwarded_sender_sections(
    body: str,
    *,
    domain_aliases: Mapping[str, str],
) -> tuple[tuple[tuple[str, str], str], ...]:
    lines = body.splitlines()
    indexes = _forwarded_header_indexes(lines)
    senders: list[tuple[tuple[str, str], str]] = []
    for position, index in enumerate(indexes):
        match = _HEADER_LINE_PATTERN.match(lines[index])
        if match is None:
            continue
        sender = _first_mailbox(
            [match.group(2)],
            domain_aliases=domain_aliases,
        )
        if sender is not None:
            end_index = (
                indexes[position + 1]
                if position + 1 < len(indexes)
                else len(lines)
            )
            senders.append(
                (sender, _forwarded_message_body(lines, index, end_index))
            )
    return tuple(senders)


def _forwarded_header_indexes(lines: list[str]) -> tuple[int, ...]:
    return tuple(
        index
        for index, line in enumerate(lines)
        if (match := _HEADER_LINE_PATTERN.match(line)) is not None
        and match.group(1).casefold() == "from"
        and _is_forwarded_header_block(lines, index)
    )


def _top_level_message_body(body: str) -> str:
    lines = body.splitlines()
    indexes = _forwarded_header_indexes(lines)
    end_index = indexes[0] if indexes else len(lines)
    return "\n".join(lines[:end_index])


def _forwarded_message_body(
    lines: list[str],
    from_index: int,
    end_index: int,
) -> str:
    body_start = from_index + 1
    while body_start < end_index:
        line = lines[body_start]
        if not line.strip():
            body_start += 1
            break
        if _HEADER_LINE_PATTERN.match(line) is None:
            break
        body_start += 1
    return "\n".join(lines[body_start:end_index])


def _is_forwarded_header_block(lines: list[str], from_index: int) -> bool:
    marker_start = max(0, from_index - 3)
    if any(
        _FORWARDED_MARKER_PATTERN.match(lines[index])
        for index in range(marker_start, from_index)
    ):
        return True

    header_names: set[str] = set()
    for line in lines[from_index + 1 : from_index + 8]:
        if not line.strip():
            break
        match = _HEADER_LINE_PATTERN.match(line)
        if match is not None:
            header_names.add(match.group(1).casefold())
    return "subject" in header_names and bool(header_names & {"sent", "date", "to"})


def _first_mailbox(
    values: list[str],
    *,
    domain_aliases: Mapping[str, str],
) -> tuple[str, str] | None:
    for _display_name, raw_address in getaddresses(values):
        local_part, separator, raw_domain = raw_address.strip().rpartition("@")
        if not separator or not local_part:
            continue
        normalized_host = _normalize_host_domain(raw_domain)
        domain = normalize_domain(normalized_host)
        if normalized_host is None or domain is None:
            continue
        domain = domain_aliases.get(normalized_host, domain)
        return f"{local_part.casefold()}@{normalized_host}", domain
    return None


def _normalize_domain_aliases(values: Mapping[str, str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for raw_source, raw_target in values.items():
        source_host = _normalize_host_domain(raw_source)
        target_domain = normalize_domain(raw_target)
        if source_host is None or target_domain is None:
            continue
        aliases[source_host] = target_domain
    return aliases


def _signature_website_domains(body: str) -> tuple[str, ...]:
    domains: list[str] = []
    for line in body.splitlines():
        if len(line) > _MAX_SIGNATURE_WEBSITE_LINE_LENGTH:
            continue
        match = _SIGNATURE_WEBSITE_LINE_PATTERN.match(line)
        if match is None:
            continue
        for raw_candidate in _WEBSITE_CANDIDATE_PATTERN.findall(match.group(1)):
            domain = _website_candidate_domain(raw_candidate)
            if domain is not None and domain not in domains:
                domains.append(domain)
    return tuple(domains)


def _website_candidate_domain(
    raw_candidate: str,
    *,
    depth: int = 0,
) -> str | None:
    if depth > _MAX_WRAPPED_URL_DEPTH:
        return None
    candidate = raw_candidate.strip().rstrip(".,;:!?]}\"'")
    if not candidate:
        return None
    if not re.match(r"^https?://", candidate, re.IGNORECASE):
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or host is None:
        return None
    normalized_host = _normalize_host_domain(host)
    if normalized_host is None:
        return None

    wrapped_parameter = _wrapped_url_parameter(normalized_host)
    if wrapped_parameter is not None:
        try:
            parameters = parse_qs(
                parsed.query,
                keep_blank_values=False,
                max_num_fields=32,
            )
        except ValueError:
            return None
        targets = parameters.get(wrapped_parameter, [])
        if len(targets) != 1:
            return None
        return _website_candidate_domain(targets[0], depth=depth + 1)

    return normalize_domain(normalized_host)


def _wrapped_url_parameter(host: str) -> str | None:
    if host == "linkprotect.cudasvc.com":
        return "a"
    if host == "safelinks.protection.outlook.com" or host.endswith(
        ".safelinks.protection.outlook.com"
    ):
        return "url"
    return None


def _is_internal(domain: str, internal_domains: frozenset[str]) -> bool:
    return domain in internal_domains


def normalize_company(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if normalized.casefold() in {"", "n/a", "none", "not provided", "null"}:
        return None
    return normalized
