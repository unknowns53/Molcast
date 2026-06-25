"""Tests for §9.4 (response_url POST with bounded retries)."""
from __future__ import annotations

from unittest import mock

import httpx
import pytest

from app import slack_dispatch


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Eliminate retry sleep so tests stay fast."""
    monkeypatch.setattr(slack_dispatch.time, "sleep", lambda *_: None)


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status_code=status, request=httpx.Request("POST", "https://x"))


def test_success_returns_true_and_does_not_retry():
    with mock.patch.object(slack_dispatch.httpx, "post", return_value=_resp(200)) as p:
        ok = slack_dispatch.post_to_response_url("https://hooks.example/x", {"text": "hi"})
    assert ok is True
    assert p.call_count == 1


def test_4xx_returns_false_and_does_not_retry():
    with mock.patch.object(slack_dispatch.httpx, "post", return_value=_resp(404)) as p:
        ok = slack_dispatch.post_to_response_url("https://hooks.example/x", {"text": "hi"})
    assert ok is False
    assert p.call_count == 1


def test_5xx_retries_up_to_max_retries():
    with mock.patch.object(slack_dispatch.httpx, "post", return_value=_resp(503)) as p:
        ok = slack_dispatch.post_to_response_url(
            "https://hooks.example/x", {"text": "hi"}, max_retries=2
        )
    assert ok is False
    # 1 initial + 2 retries = 3 calls.
    assert p.call_count == 3


def test_5xx_then_success_returns_true():
    responses = [_resp(503), _resp(200)]
    with mock.patch.object(slack_dispatch.httpx, "post", side_effect=responses) as p:
        ok = slack_dispatch.post_to_response_url(
            "https://hooks.example/x", {"text": "hi"}, max_retries=2
        )
    assert ok is True
    assert p.call_count == 2


def test_connection_error_retries_then_gives_up():
    err = httpx.ConnectError("nope", request=httpx.Request("POST", "https://x"))
    with mock.patch.object(slack_dispatch.httpx, "post", side_effect=err) as p:
        ok = slack_dispatch.post_to_response_url(
            "https://hooks.example/x", {"text": "hi"}, max_retries=2
        )
    assert ok is False
    assert p.call_count == 3
