"""POST the final result to Slack ``response_url`` with bounded retries (§9.4).

§5.2 caps the retry budget at 2 (3 total attempts) with exponential
backoff to stay well under Slack's 30-minute / 5-call response_url
window. Only 5xx and network-level errors trigger retries; 4xx responses
are surfaced and not retried.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)


_RETRYABLE_STATUS = {500, 502, 503, 504}


def post_to_response_url(
    response_url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 5.0,
    max_retries: int = 2,
    base_backoff: float = 0.5,
) -> bool:
    """Return True on a successful (<500, non-network-error) POST.

    On retryable failure (5xx, ConnectError, ReadError, TimeoutException)
    we sleep ``base_backoff * 2**attempt`` between attempts. On terminal
    failure we log the last error kind and return False. The caller is
    a Slack BackgroundTask, so a False return is logged only; we do not
    raise.
    """
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(response_url, json=payload, timeout=timeout)
        except (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.TimeoutException,
        ) as exc:
            last_error = type(exc).__name__
        else:
            if resp.status_code < 400:
                return True
            if resp.status_code not in _RETRYABLE_STATUS:
                logger.error(
                    "response_url POST refused",
                    extra={"status_code": resp.status_code, "attempt": attempt},
                )
                return False
            last_error = f"HTTP_{resp.status_code}"

        if attempt < max_retries:
            time.sleep(base_backoff * (2 ** attempt))

    logger.error(
        "response_url POST failed after retries",
        extra={"error_kind": last_error, "attempt": max_retries + 1},
    )
    return False
