"""Tests for §6.4 (Slack signing verification)."""
from __future__ import annotations

import hashlib
import hmac
import time

from app.slack_verify import verify_slack_request


SECRET = "shhh-this-is-a-test-secret"


def _signed(body: bytes, *, ts: int | None = None, secret: str = SECRET) -> tuple[str, str]:
    if ts is None:
        ts = int(time.time())
    base = b"v0:" + str(ts).encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return (str(ts), "v0=" + digest)


def test_valid_signature_accepted():
    body = b"token=xxx&text=CCO"
    ts, sig = _signed(body)
    assert verify_slack_request(SECRET, ts, sig, body) is True


def test_wrong_signature_rejected():
    body = b"token=xxx&text=CCO"
    ts, _ = _signed(body)
    assert verify_slack_request(SECRET, ts, "v0=" + "0" * 64, body) is False


def test_tampered_body_rejected():
    body_a = b"token=xxx&text=CCO"
    body_b = b"token=xxx&text=ESCAPE"
    ts, sig = _signed(body_a)
    assert verify_slack_request(SECRET, ts, sig, body_b) is False


def test_old_timestamp_rejected():
    body = b"token=xxx&text=CCO"
    ts, sig = _signed(body, ts=int(time.time()) - 10 * 60)
    assert verify_slack_request(SECRET, ts, sig, body) is False


def test_future_timestamp_rejected():
    body = b"token=xxx&text=CCO"
    ts, sig = _signed(body, ts=int(time.time()) + 10 * 60)
    assert verify_slack_request(SECRET, ts, sig, body) is False


def test_missing_inputs_rejected():
    assert verify_slack_request("", "1", "v0=00", b"x") is False
    assert verify_slack_request(SECRET, "", "v0=00", b"x") is False
    assert verify_slack_request(SECRET, "1", "", b"x") is False


def test_non_numeric_timestamp_rejected():
    body = b"x"
    assert verify_slack_request(SECRET, "not-a-number", "v0=00", body) is False


def test_wrong_secret_rejected():
    body = b"token=xxx&text=CCO"
    ts, sig = _signed(body, secret="WRONG")
    assert verify_slack_request(SECRET, ts, sig, body) is False
