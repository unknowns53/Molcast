"""Tests for Cloud Tasks dispatch (§5.2 of the design brief).

The CloudTasksClient instance is replaced at module scope with a
``mock.MagicMock`` so neither GCP credentials nor network are needed.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest
from google.api_core.exceptions import AlreadyExists

from app import tasks_dispatch
from app.config import Settings
from app.tasks_dispatch import TasksConfigError


_BASE_URL = "https://mol-slack-viewer-xxxxxxxx-an.a.run.app"
_RESPONSE_URL = (
    "https://hooks.slack.com/commands/T000/123/abc"
)


@pytest.fixture
def settings():
    return Settings(
        TASKS_QUEUE_ID="molcast-name-resolution",
        TASKS_LOCATION="asia-northeast1",
        TASKS_PROJECT_ID="example-proj",
        TASKS_INVOKER_SA="molcast-ct-invoker@example-proj.iam.gserviceaccount.com",
        INTERNAL_PROCESS_PATH="/internal/process",
    )


@pytest.fixture(autouse=True)
def _reset_client():
    """Tests may patch ``_client`` directly; reset between tests."""
    tasks_dispatch._client = None
    yield
    tasks_dispatch._client = None


# ---------------------------------------------------------------------------
# Config guards
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field,blank_value",
    [
        ("TASKS_PROJECT_ID", ""),
        ("TASKS_QUEUE_ID", ""),
        ("TASKS_INVOKER_SA", ""),
    ],
)
def test_missing_required_env_raises_config_error(field, blank_value, settings):
    """Empty required env values must surface as TasksConfigError
    BEFORE any client call. Local dev / unit tests boot with these
    blank, so accidentally hitting the dispatch path must fail fast.
    """
    setattr(settings, field, blank_value)
    with pytest.raises(TasksConfigError):
        tasks_dispatch.enqueue_name_resolution(
            name="ethanol",
            response_url=_RESPONSE_URL,
            user_id=None,
            channel_id=None,
            base_url=_BASE_URL,
            settings=settings,
        )


def test_blank_base_url_raises_config_error(settings):
    with pytest.raises(TasksConfigError):
        tasks_dispatch.enqueue_name_resolution(
            name="ethanol",
            response_url=_RESPONSE_URL,
            user_id=None,
            channel_id=None,
            base_url="",
            settings=settings,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_enqueue_builds_correct_task(settings):
    """``create_task`` is called once with a deterministic task name,
    the right OIDC audience, and a JSON body that round-trips.
    """
    fake_client = mock.MagicMock()
    fake_client.queue_path.return_value = (
        "projects/example-proj/locations/asia-northeast1/queues/molcast-name-resolution"
    )
    with mock.patch.object(tasks_dispatch, "_client", fake_client):
        task_name = tasks_dispatch.enqueue_name_resolution(
            name="hexafluorobenzene",
            response_url=_RESPONSE_URL,
            user_id="U123",
            channel_id="C456",
            base_url=_BASE_URL,
            settings=settings,
        )

    assert fake_client.create_task.call_count == 1
    request_kwargs = fake_client.create_task.call_args.kwargs["request"]
    parent = request_kwargs["parent"]
    task = request_kwargs["task"]
    assert parent == (
        "projects/example-proj/locations/asia-northeast1/queues/molcast-name-resolution"
    )

    # Deterministic task name: sha256(response_url) truncated to 32 chars.
    assert task_name.startswith(
        "projects/example-proj/locations/asia-northeast1/queues/molcast-name-resolution/tasks/"
    )
    assert len(task_name.rsplit("/", 1)[-1]) == 32

    # OIDC audience must equal base_url; target URL must end with the
    # configured internal path.
    assert task.http_request.url == f"{_BASE_URL}/internal/process"
    assert task.http_request.oidc_token.audience == _BASE_URL
    assert (
        task.http_request.oidc_token.service_account_email
        == "molcast-ct-invoker@example-proj.iam.gserviceaccount.com"
    )

    body = json.loads(task.http_request.body)
    assert body["kind"] == "name"
    assert body["payload"] == "hexafluorobenzene"
    assert body["response_url"] == _RESPONSE_URL
    assert body["user_id"] == "U123"
    assert body["channel_id"] == "C456"
    assert body["base_url"] == _BASE_URL
    assert body["schema_version"] == 1
    assert body["idempotency_key"] == task_name.rsplit("/", 1)[-1]


def test_enqueue_swallows_already_exists(settings):
    """Slack retries trigger a second enqueue with the same task name;
    the second call hits ``AlreadyExists`` and must be a no-op (the
    first task already in the queue will do the work).
    """
    fake_client = mock.MagicMock()
    fake_client.queue_path.return_value = "projects/x/locations/y/queues/z"
    fake_client.create_task.side_effect = AlreadyExists("dup")
    with mock.patch.object(tasks_dispatch, "_client", fake_client):
        # Must NOT raise.
        task_name = tasks_dispatch.enqueue_name_resolution(
            name="ethanol",
            response_url=_RESPONSE_URL,
            user_id=None,
            channel_id=None,
            base_url=_BASE_URL,
            settings=settings,
        )
    assert task_name.endswith(tasks_dispatch._idempotency_key(_RESPONSE_URL))


def test_different_response_urls_produce_different_task_names(settings):
    """Two different ``response_url`` values must produce different
    task names — otherwise a heavily-used user would have their
    second slash invocation silently dropped.
    """
    key_a = tasks_dispatch._idempotency_key(_RESPONSE_URL)
    key_b = tasks_dispatch._idempotency_key(_RESPONSE_URL + "?nonce=1")
    assert key_a != key_b
    assert len(key_a) == 32
    assert len(key_b) == 32
