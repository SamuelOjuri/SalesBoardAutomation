"""Structured Monday failures and server-directed retry deadlines."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any


class MondayAPIError(RuntimeError):
    """A rejected or malformed response; unknown GraphQL failures fail closed."""

    retryable = False
    defer_attempt = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        codes: tuple[str, ...] = (),
        status_code: int | None = None,
        request_id: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.codes = codes or ((code,) if code else ())
        self.status_code = status_code
        self.request_id = request_id
        self.retry_at = retry_at


class MondayTransientError(MondayAPIError):
    """A temporary upstream failure, subject to the worker retry budget."""

    retryable = True


class MondayRateLimitError(MondayTransientError):
    """Capacity exhaustion defers durable work without spending an attempt."""

    defer_attempt = True


class MondayCooldownError(MondayRateLimitError):
    """A request was prevented by a previously recorded cooldown."""


_PERMANENT_CODES = {
    "request_max_complexity_exceeded", "maxcomplexityexceeded",
    "userunauthorizedexception", "user_access_denied", "unauthorized",
    "missingrequiredpermissions", "graphql_validation_failed", "graphql_parse_failed",
    "columnvalueexception", "correctedvalueexception", "invalidargumentexception",
    "invalidboardidexception", "invalidcolumnidexception", "invaliduseridexception",
    "invalidversionexception", "resourcenotfoundexception", "jsonparseexception",
}
_LIMIT_CODES = {
    "daily_limit_exceeded", "complexity_budget_exhausted", "complexityexception",
    "maxconcurrencyexceeded", "ip_rate_limit_exceeded", "rate_limit_exceeded",
    "rate limit exceeded", "minute limit rate exceeded", "concurrency limit exceeded",
}
_TRANSIENT_CODES = {
    "api_temporarily_blocked", "internal_server_error", "internalservererror",
    "idempotency_conflict",
}


def response_error(
    status_code: int,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    now: datetime,
) -> MondayAPIError:
    """Read both current GraphQL extensions and legacy Monday error envelopes.

    Never put upstream messages or error_data (which can contain inputs) in
    exception text. Retain all codes, even for mixed/partial GraphQL failures.
    """
    now = now.astimezone(timezone.utc)
    raw_errors = payload.get("errors")
    errors = raw_errors if isinstance(raw_errors, list) and raw_errors else [payload]
    codes: list[str] = []
    kinds: list[str] = []
    for error in errors:
        if not isinstance(error, Mapping):
            kinds.append("permanent")
            continue
        extensions = error.get("extensions")
        extensions = extensions if isinstance(extensions, Mapping) else {}
        code = extensions.get("code") or error.get("error_code") or error.get("code")
        if isinstance(code, str):
            codes.append(code)
        normalized = code.casefold() if isinstance(code, str) else ""
        status = extensions.get("status_code", error.get("status_code", status_code))
        if (
            normalized in _PERMANENT_CODES
            or status_code in {400, 401, 403, 404, 422}
            or status in {400, 401, 403, 404, 422}
        ):
            kinds.append("permanent")
        elif normalized in _LIMIT_CODES or 429 in (status_code, status):
            kinds.append("limit")
        elif normalized in _TRANSIENT_CODES or status in {408, 423} or (
            isinstance(status, int) and 500 <= status <= 599
        ) or 500 <= status_code <= 599:
            kinds.append("transient")
        else:
            kinds.append("permanent")

    # Every error must be retryable. A limit cannot mask a bad query/permission.
    kind = "permanent" if "permanent" in kinds else (
        "limit" if "limit" in kinds else "transient"
    )
    delay = retry_after_seconds(headers, payload, now=now)
    retry_at = None
    if kind != "permanent":
        fallback_delay = 60 if kind == "limit" else 30
        retry_at = now + timedelta(seconds=delay if delay is not None else fallback_delay)
        if any(code.casefold() == "daily_limit_exceeded" for code in codes):
            midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            retry_at = max(retry_at, midnight)
    extensions = payload.get("extensions")
    extensions = extensions if isinstance(extensions, Mapping) else {}
    request_id = extensions.get("request_id") or payload.get("request_id")
    error_class = {
        "permanent": MondayAPIError,
        "limit": MondayRateLimitError,
        "transient": MondayTransientError,
    }[kind]
    return error_class(
        "Monday request failed",
        code=codes[0] if codes else f"http_{status_code}",
        codes=tuple(codes),
        status_code=status_code,
        request_id=request_id if isinstance(request_id, str) else None,
        retry_at=retry_at,
    )


def retry_after_seconds(
    headers: Mapping[str, str], payload: Mapping[str, Any], *, now: datetime
) -> float | None:
    delays: list[float] = []

    def add(value: object) -> bool:
        if isinstance(value, bool):
            return False
        try:
            delay = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        # Limit to representable datetime arithmetic, not worker backoff caps.
        if math.isfinite(delay) and 0 <= delay <= (datetime.max.replace(
            tzinfo=timezone.utc
        ) - now).total_seconds() - 1:
            delays.append(delay)
            return True
        return False

    header = next((v for k, v in headers.items() if k.casefold() == "retry-after"), None)
    if header is not None and not add(header):
        try:
            deadline = parsedate_to_datetime(header)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            add(max(0, (deadline - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            add(value.get("retry_in_seconds"))
            # Only error metadata, never recurse into successful query data.
            for key in ("errors", "extensions", "error_data"):
                visit(value.get(key))
        elif isinstance(value, list):
            for entry in value:
                visit(entry)

    visit(payload)
    return max(delays) if delays else None
