"""Tests for the local-dev inline name-resolution runner.

Cloud Tasks is mocked out at the call site (test_main.py covers the
swap); here we focus on the worker behavior:

  * happy path: OPSIN → SMILES → _process_smiles_sync → response_url POST
  * OPSIN user error: error payload posted to response_url, no
    _process_smiles_sync call
  * OPSIN unexpected error: generic fallback message posted, no
    _process_smiles_sync call

Threading is collapsed to synchronous execution by patching
``threading.Thread`` with a fake that invokes the kwargs target inline.
"""
from __future__ import annotations

from unittest import mock

import pytest

from app import dev_dispatch
from app.config import reload_settings
from app.rdkit_utils import MoleculeGenerationError


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


def _call_kwargs(name: str = "hexafluorobenzene") -> dict:
    from app.config import get_settings

    return {
        "name": name,
        "response_url": _RESPONSE_URL,
        "user_id": "U1",
        "channel_id": "C1",
        "base_url": _BASE_URL,
        "settings": get_settings(),
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
    # The fake thread instance's start() must have been called.
    mthread.return_value.start.assert_called_once()


def test_run_inline_happy_path_resolves_and_posts_to_response_url():
    """End-to-end (sync-thread): OPSIN returns a SMILES,
    _process_smiles_sync builds the viewer payload, response_url POST
    runs.
    """
    fake_payload = {
        "response_type": "ephemeral",
        "replace_original": False,
        "text": "3D viewer generated: https://x/view/abc",
    }
    # Patch app.main._process_smiles_sync via the late-import boundary.
    # The thread target imports it inside the function body, so we patch
    # it at the source module.
    with mock.patch.object(dev_dispatch.threading, "Thread", _SyncThread), \
         mock.patch.object(
            dev_dispatch, "iupac_to_smiles", return_value="Fc1c(F)c(F)c(F)c(F)c1F"
         ) as mopsin, \
         mock.patch(
            "app.main._process_smiles_sync", return_value=fake_payload
         ) as mproc, \
         mock.patch.object(
            dev_dispatch.slack_dispatch, "post_to_response_url", return_value=True
         ) as mpost:
        ret = dev_dispatch.run_inline_name_resolution(**_call_kwargs())
    assert ret.startswith("dev-inline-task/")
    assert mopsin.call_count == 1
    assert mproc.call_count == 1
    proc_kwargs = mproc.call_args.kwargs
    assert proc_kwargs["smiles"] == "Fc1c(F)c(F)c(F)c(F)c1F"
    assert proc_kwargs["input_name"] == "hexafluorobenzene"
    assert mpost.call_count == 1
    posted_url, posted_payload = mpost.call_args.args
    assert posted_url == _RESPONSE_URL
    assert posted_payload == fake_payload


def test_run_inline_opsin_user_error_posts_error_message():
    """OPSIN MoleculeGenerationError must propagate to a Slack-friendly
    text and NOT call the SMILES pipeline.
    """
    with mock.patch.object(dev_dispatch.threading, "Thread", _SyncThread), \
         mock.patch.object(
            dev_dispatch,
            "iupac_to_smiles",
            side_effect=MoleculeGenerationError("OPSIN は体系名のみ対応です..."),
         ), \
         mock.patch(
            "app.main._process_smiles_sync"
         ) as mproc, \
         mock.patch.object(
            dev_dispatch.slack_dispatch, "post_to_response_url", return_value=True
         ) as mpost:
        dev_dispatch.run_inline_name_resolution(**_call_kwargs())
    assert mproc.call_count == 0
    assert mpost.call_count == 1
    _, posted_payload = mpost.call_args.args
    assert "OPSIN" in posted_payload["text"]


def test_run_inline_opsin_unexpected_error_posts_generic_message():
    """An unexpected (non-user) exception from OPSIN must be caught so
    the thread does not silently die; a generic message is sent to
    Slack.
    """
    with mock.patch.object(dev_dispatch.threading, "Thread", _SyncThread), \
         mock.patch.object(
            dev_dispatch,
            "iupac_to_smiles",
            side_effect=RuntimeError("subprocess crashed"),
         ), \
         mock.patch(
            "app.main._process_smiles_sync"
         ) as mproc, \
         mock.patch.object(
            dev_dispatch.slack_dispatch, "post_to_response_url", return_value=True
         ) as mpost:
        dev_dispatch.run_inline_name_resolution(**_call_kwargs())
    assert mproc.call_count == 0
    assert mpost.call_count == 1
    _, posted_payload = mpost.call_args.args
    assert "OPSIN" in posted_payload["text"]
    assert "エラー" in posted_payload["text"]
