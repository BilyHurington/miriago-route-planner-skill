"""Bounded HTTP helpers for public import sources."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_USER_AGENT = (
    "plan-miriago-route/1.7 "
    "(https://github.com/bilyhurington/MiriaGo)"
)


class NetworkError(ValueError):
    """Raised when a public source cannot be fetched safely."""


def fetch_bytes(
    request: str | Request,
    *,
    timeout_seconds: float = 30,
    max_bytes: int = 64 * 1024 * 1024,
    requester: Callable[..., Any] = urlopen,
) -> bytes:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    value = request
    if isinstance(request, str):
        value = Request(request, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        response = requester(value, timeout=timeout_seconds)
        with response:
            status = getattr(response, "status", 200)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise NetworkError(f"HTTP request returned status {status}.")
            content_length = _content_length(getattr(response, "headers", None))
            if content_length is not None and content_length > max_bytes:
                raise NetworkError(
                    f"HTTP response is too large ({content_length} bytes; "
                    f"limit {max_bytes})."
                )
            data = response.read(max_bytes + 1)
    except NetworkError:
        raise
    except HTTPError as exc:
        raise NetworkError(f"HTTP request failed with status {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise NetworkError(f"HTTP request failed: {exc}") from exc
    if len(data) > max_bytes:
        raise NetworkError(f"HTTP response exceeds the {max_bytes}-byte limit.")
    return data


def fetch_json(
    request: str | Request,
    *,
    timeout_seconds: float = 30,
    max_bytes: int = 16 * 1024 * 1024,
    requester: Callable[..., Any] = urlopen,
) -> Any:
    data = fetch_bytes(
        request,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        requester=requester,
    )
    try:
        return json.loads(data.decode("utf-8-sig"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise NetworkError(f"HTTP response is not valid UTF-8 JSON: {exc}") from exc


def _content_length(headers: Any) -> int | None:
    if headers is None:
        return None
    try:
        value = headers.get("Content-Length")
    except AttributeError:
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")
