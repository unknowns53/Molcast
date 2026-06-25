"""Slack request signing verification (HMAC-SHA256, see §6.4)."""
from __future__ import annotations

import hashlib
import hmac
import time


def verify_slack_request(
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    tolerance_seconds: int = 300,
) -> bool:
    """Return True iff the Slack signature is valid and the timestamp is fresh.

    The basestring is ``v0:{timestamp}:{raw_body}`` per Slack's docs;
    comparison uses ``hmac.compare_digest`` to avoid timing leaks.

    A ``False`` return covers every failure mode (missing input, bad
    timestamp, replay outside ``tolerance_seconds``, mismatched digest).
    Callers should respond with 403 in that case.
    """
    if not signing_secret or not timestamp or not signature:
        return False

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False

    if abs(int(time.time()) - ts) > tolerance_seconds:
        return False

    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()
    expected = "v0=" + digest
    return hmac.compare_digest(expected, signature)
