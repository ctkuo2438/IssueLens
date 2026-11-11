"""Identify temporary provider failures and interpret their waiting periods."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import math
import socket
import ssl

from openai import APIConnectionError, APIStatusError, APITimeoutError


TEMPORARY_HTTP_STATUSES = {408, 500, 502, 503, 504}
MIN_RETRY_DELAY = 1.0


@dataclass(frozen=True)
class TransientFailure:
    reason: str
    delay: float = MIN_RETRY_DELAY


def nonnegative_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    seconds = nonnegative_seconds(value)
    if seconds is not None or value is None:
        return seconds
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def connection_can_retry(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, (ssl.SSLError, ValueError)):
            return False
        if isinstance(cause, socket.gaierror) and cause.errno != socket.EAI_AGAIN:
            return False
        cause = cause.__cause__
    return True


def openai_transient_failure(error: APIConnectionError | APIStatusError) -> TransientFailure | None:
    if isinstance(error, APITimeoutError):
        return TransientFailure("the model request timed out")
    if isinstance(error, APIConnectionError):
        return TransientFailure("the connection to OpenAI failed") if connection_can_retry(error) else None

    headers = error.response.headers
    if headers.get("x-should-retry", "").casefold() == "false":
        return None
    delay = retry_after_seconds(headers)
    milliseconds = nonnegative_seconds(headers.get("retry-after-ms"))
    delay = max(MIN_RETRY_DELAY, delay or 0, (milliseconds or 0) / 1000)
    if error.status_code == 429:
        # Billing and monthly usage limits also use 429. Only retry a known throttle.
        throttled = error.code in {"rate_limit_exceeded", "slow_down"} or (
            error.code is None and error.type == "rate_limit_error"
        )
        if throttled and error.type != "insufficient_quota":
            return TransientFailure("OpenAI is temporarily rate limiting requests", delay)
    elif error.status_code in TEMPORARY_HTTP_STATUSES:
        return TransientFailure(f"OpenAI returned temporary HTTP {error.status_code}", delay)
    return None
