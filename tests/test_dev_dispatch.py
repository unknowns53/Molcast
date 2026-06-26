"""Tests for the local-dev inline trajectory resolver.

Cloud Tasks is mocked out at the call site (test_main.py covers the
swap); here we focus on the worker behavior:

  * happy path: spawns a thread, runs process_and_save_frames, posts to
    response_url.
  * unexpected error in the pipeline must NOT kill the thread silently;
    a generic fallback message is posted instead.

Threading is collapsed to synchronous execution by patching
``threading.Thread`` with a fake that invokes the kwargs target inline.
"""
from __future__ import annotations

from unittest import mock

import pytest

from app import dev_dispatch
from app.config import reload_settings


_RESPONSE_URL = "https://hooks.slack.com/commands/T0/1/abc"
_BASE_URL = "https://example.test"


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "dummy")
    reload_settings()
    yield
    reload_settings()


class _SyncThread:
    """Drop-in replacement for ``threading.Thread`` that runs ``target``
    inline when ``start()`` is called. Lets us assert on side effects
    without race conditions.
    """

    def __init__(self, *, target, kwargs, daemon, name):
        self._target = target
        self._kwargs = kwargs
        self._daemon = daemon
        self._name = name

    def start(self) -> None:
        self._target(**self._kwargs)


def _call_kwargs() -> dict:
    from app.config import get_settings

    return {
        "segments": [("name", "hexafluorobenzene")],
        "was_capped": False,
        "response_url": _RESPONSE_URL,
        "user_id": "U1",
        "channel_id": "C1",
        "base_url": _BASE_URL,
        "settings": get_settings(),
        "flags": {"public": False, "label": False, "no_3d": False},
    }


def test_run_inline_spawns_thread_and_returns_synthetic_task_name():
    """The synthetic task_name embeds the idempotency key (sha256 prefix
    of response_url) so logs can correlate with the prod CT name.
    """
    with mock.patch.object(dev_dispatch.threading, "Thread") as mthread:
        mthread.return_value = mock.MagicMock()
        ret = dev_dispatch.run_inline_name_resolution(**_call_kwargs())
    assert ret.startswith("dev-inline-task/")
    assert mthread.call_count == 1
    mthread.return_value.start.assert_called_once()


def test_run_inline_happy_path_calls_process_and_posts():
    """End-to-end (sync-thread): process_and_save_frames returns a
    payload, response_url POST runs once with that payload."""
    fake_payload = {
        "response_type": "ephemeral",
        "replace_original": False,
        "text": "ok",
    }
    with mock.patch.object(dev_dispatch.threading, "Thread", _SyncThread), \
         mock.patch(
            "app.main.process_and_save_frames", return_value=fake_payload
         ) as mproc, \
         mock.patch.object(
            dev_dispatch.slack_dispatch, "post_to_response_url", return_value=True
         ) as mpost:
        dev_dispatch.run_inline_name_resolution(**_call_kwargs())
    assert mproc.call_count == 1
    pkw = mproc.call_args.kwargs
    assert pkw["segments"] == [("name", "hexafluorobenzene")]
    assert pkw["was_capped"] is False
    assert pkw["base_url"] == _BASE_URL
    assert mpost.call_count == 1
    posted_url, posted_payload = mpost.call_args.args
    assert posted_url == _RESPONSE_URL
    assert posted_payload == fake_payload


def test_run_inline_unexpected_error_posts_generic_message():
    """An unexpected exception from the pipeline must be caught so the
    thread does not silently die; a generic message goes to Slack."""
    with mock.patch.object(dev_dispatch.threading, "Thread", _SyncThread), \
         mock.patch(
            "app.main.process_and_save_frames",
            side_effect=RuntimeError("boom"),
         ), \
         mock.patch.object(
            dev_dispatch.slack_dispatch, "post_to_response_url", return_value=True
         ) as mpost:
        dev_dispatch.run_inline_name_resolution(**_call_kwargs())
    assert mpost.call_count == 1
    _, posted_payload = mpost.call_args.args
    assert "fail" in posted_payload["text"].lower()
