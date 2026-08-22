"""Deterministic requester identity extraction from trusted email structure."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from email import policy
from email.parser import Parser
from email.utils import getaddresses
from typing import Literal

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
_DOMAIN_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())


RequesterSource = Literal["top_level_sender", "forwarded_sender", "not_found"]


@dataclass(frozen=True, slots=True)
class RequesterIdentity:
    email_address: str | None
    domain: str | None
    company: str | None
    source: RequesterSource


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
        return _identity(top_level_sender, company, "top_level_sender")

    for sender in _forwarded_senders(
        parsed_email.body,
        domain_aliases=normalized_domain_aliases,
    ):
        if not _is_internal(sender[1], normalized_internal_domains):
            return _identity(sender, company, "forwarded_sender")

    return RequesterIdentity(
        email_address=None,
        domain=None,
        company=company,
        source="not_found",
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
) -> RequesterIdentity:
    email_address, sender_domain = sender
    domain = None if sender_domain in _GENERIC_EMAIL_DOMAINS else sender_domain
    return RequesterIdentity(
        email_address=email_address,
        domain=domain,
        company=company,
        source=source,
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


def _forwarded_senders(
    body: str,
    *,
    domain_aliases: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    lines = body.splitlines()
    senders: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        match = _HEADER_LINE_PATTERN.match(line)
        if match is None or match.group(1).casefold() != "from":
            continue
        if not _is_forwarded_header_block(lines, index):
            continue
        sender = _first_mailbox(
            [match.group(2)],
            domain_aliases=domain_aliases,
        )
        if sender is not None:
            senders.append(sender)
    return tuple(senders)


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


def _is_internal(domain: str, internal_domains: frozenset[str]) -> bool:
    return domain in internal_domains


def normalize_company(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if normalized.casefold() in {"", "n/a", "none", "not provided", "null"}:
        return None
    return normalized
