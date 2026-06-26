r"""Cloud Tasks dispatch for the multi-segment ``/mol`` trajectory flow
(see §5.2, §8.2 of the design brief).

``/slack/mol`` enqueues a task here, returns ack within Slack's 3 s
window, and ``/internal/process`` consumes the task asynchronously. The
task carries the parsed ``segments`` list AND the ``response_url`` so
the worker can post the final viewer URL (or an error message) back to
Slack via ``app.slack_dispatch.post_to_response_url``.

Two design decisions worth restating:

  1. **Deterministic task name** =
     ``projects/{p}/locations/{l}/queues/{q}/tasks/{sha256(response_url)[:32]}``.
     Slack retries the slash command on its own timeout; the second
     enqueue with the same ``response_url`` collides on the name and
     Cloud Tasks raises ``AlreadyExists``, which we swallow.
  2. **OIDC audience** = ``base_url`` (Cloud Run service origin only).
     ``id_token.verify_oauth2_token`` then runs against the same
     origin, so the audience values used at enqueue and verify sides
     must always match exactly — a single source-of-truth env
     (``BASE_URL``) reduces drift.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from .config import Settings


logger = logging.getLogger("molcast.tasks")


_client: tasks_v2.CloudTasksClient | None = None


def _get_client() -> tasks_v2.CloudTasksClient:
    """Lazy singleton — mirrors ``app.store._client`` style. Default
    credentials are picked up via ADC at first use; in unit tests the
    client is patched at the module level so this path never runs.
    """
    global _client
    if _client is None:
        _client = tasks_v2.CloudTasksClient()
    return _client


class TasksConfigError(Exception):
    """Raised when required ``TASKS_*`` env settings are missing.

    Surfaced as a user-friendly Slack message at the call site; the
    operator sees the real error class in structured logs.
    """


def _idempotency_key(response_url: str) -> str:
    """``sha256(response_url)[:32]`` — Slack's ``response_url`` is per
    request and per user, so this gives us a clean per-Slack-invocation
    dedup key shared by both the Cloud Tasks task name and the
    Firestore idempotency document.
    """
    return hashlib.sha256(response_url.encode("utf-8")).hexdigest()[:32]


def _build_payload(
    *,
    segments: list[tuple[str, str]],
    was_capped: bool,
    response_url: str,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    idempotency_key: str,
    flags: dict[str, bool] | None = None,
) -> bytes:
    """JSON payload for ``/internal/process`` (schema v2).

    Segments are emitted as ``[{kind, payload}, ...]`` rather than a
    plain string so the worker can distinguish ``smiles`` from ``name``
    per element without re-parsing — the user's original token boundary
    is the source of truth.
    """
    body: dict[str, Any] = {
        "schema_version": 2,
        "kind": "trajectory",
        "segments": [{"kind": k, "payload": p} for k, p in segments],
        "was_capped": bool(was_capped),
        "response_url": response_url,
        "user_id": user_id,
        "channel_id": channel_id,
        "base_url": base_url,
        "idempotency_key": idempotency_key,
        "flags": flags or {"public": False, "label": False, "no_3d": False},
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def enqueue_name_resolution(
    *,
    segments: list[tuple[str, str]],
    was_capped: bool,
    response_url: str,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    settings: Settings,
    flags: dict[str, bool] | None = None,
) -> str:
    """Enqueue a trajectory task. Returns the task name string (so
    callers can log the resource identifier). Raises
    :class:`TasksConfigError` when required env settings are missing.

    Function name kept as ``enqueue_name_resolution`` for call-site
    stability; semantically it now handles the full segment list (any
    mix of ``smiles`` / ``name``).
    """
    if not settings.TASKS_PROJECT_ID:
        raise TasksConfigError("TASKS_PROJECT_ID is empty")
    if not settings.TASKS_QUEUE_ID:
        raise TasksConfigError("TASKS_QUEUE_ID is empty")
    if not settings.TASKS_INVOKER_SA:
        raise TasksConfigError("TASKS_INVOKER_SA is empty")
    if not base_url:
        raise TasksConfigError("base_url is empty")
    if not segments:
        raise TasksConfigError("segments is empty")

    client = _get_client()
    parent = client.queue_path(
        settings.TASKS_PROJECT_ID,
        settings.TASKS_LOCATION,
        settings.TASKS_QUEUE_ID,
    )
    idempotency = _idempotency_key(response_url)
    task_name = (
        f"projects/{settings.TASKS_PROJECT_ID}"
        f"/locations/{settings.TASKS_LOCATION}"
        f"/queues/{settings.TASKS_QUEUE_ID}"
        f"/tasks/{idempotency}"
    )
    target_url = base_url.rstrip("/") + settings.INTERNAL_PROCESS_PATH

    http_req = tasks_v2.HttpRequest(
        http_method=tasks_v2.HttpMethod.POST,
        url=target_url,
        headers={"Content-Type": "application/json"},
        body=_build_payload(
            segments=segments,
            was_capped=was_capped,
            response_url=response_url,
            user_id=user_id,
            channel_id=channel_id,
            base_url=base_url,
            idempotency_key=idempotency,
            flags=flags,
        ),
        oidc_token=tasks_v2.OidcToken(
            service_account_email=settings.TASKS_INVOKER_SA,
            audience=base_url,
        ),
    )
    task = tasks_v2.Task(name=task_name, http_request=http_req)

    try:
        client.create_task(request={"parent": parent, "task": task})
    except AlreadyExists:
        logger.info(
            "tasks_already_exists",
            extra={"error_kind": "AlreadyExists"},
        )
        return task_name

    logger.info(
        "tasks_enqueued",
        extra={"segment_count": len(segments)},
    )
    return task_name
