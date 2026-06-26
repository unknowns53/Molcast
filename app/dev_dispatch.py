"""Local-dev inline replacement for Cloud Tasks dispatch.

Activated by ``Settings.DEV_INLINE_NAME_RESOLUTION``. The signature of
:func:`run_inline_name_resolution` matches
:func:`app.tasks_dispatch.enqueue_name_resolution` so :mod:`app.main`
can swap one for the other with a single branch.

Instead of enqueueing to Cloud Tasks, we spawn a daemon thread that runs
the trajectory pipeline (OPSIN → RDKit → Firestore → ``response_url``
POST) inline. The thread is fire-and-forget; failures are logged but
never raised because there is no Cloud Tasks queue to retry against in
dev.

This module must never run in production: a misconfigured Cloud Run
service that flipped the flag on would silently bypass the OIDC-guarded
``/internal/process`` worker. The flag is logged at startup
(:func:`app.main._lifespan`) so the prod log immediately surfaces the
mistake.
"""
from __future__ import annotations

import hashlib
import logging
import threading

from . import slack_dispatch
from .config import Settings


logger = logging.getLogger("molcast.dev_dispatch")


def _idempotency_key(response_url: str) -> str:
    return hashlib.sha256(response_url.encode("utf-8")).hexdigest()[:32]


def _resolve_and_post(
    *,
    segments: list[tuple[str, str]],
    was_capped: bool,
    response_url: str,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    settings: Settings,
    flags: dict[str, bool] | None = None,
) -> None:
    """Thread target. Runs the same trajectory pipeline as the Cloud
    Tasks worker. All exceptions are caught here because the caller has
    already ack'd Slack and there is nothing above this thread to
    receive a raised error.
    """
    from .main import process_and_save_frames  # late import: avoids cycle

    logger.info(
        "dev_inline_started",
        extra={"segment_count": len(segments)},
    )
    try:
        payload = process_and_save_frames(
            segments=segments,
            was_capped=was_capped,
            user_id=user_id,
            channel_id=channel_id,
            base_url=base_url,
            settings=settings,
            flags=flags,
        )
    except Exception:
        logger.exception("dev_inline_unexpected_error")
        payload = {
            "response_type": settings.SLACK_RESPONSE_TYPE,
            "replace_original": False,
            "text": "dev inline resolution failed (check Cloud Run logs).",
        }
    slack_dispatch.post_to_response_url(response_url, payload)
    logger.info(
        "dev_inline_completed",
        extra={"segment_count": len(segments)},
    )


def run_inline_name_resolution(
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
    """Spawn the resolution thread and return a synthetic ``task_name``.

    The return value is logged by the caller for parity with the Cloud
    Tasks code path; it carries no semantic meaning in dev. Function
    name kept stable so :mod:`app.main` does not need a conditional
    import.
    """
    key = _idempotency_key(response_url) if response_url else "no-response-url"
    thread = threading.Thread(
        target=_resolve_and_post,
        kwargs={
            "segments": segments,
            "was_capped": was_capped,
            "response_url": response_url,
            "user_id": user_id,
            "channel_id": channel_id,
            "base_url": base_url,
            "settings": settings,
            "flags": flags,
        },
        daemon=True,
        name=f"dev-inline-{key[:8]}",
    )
    thread.start()
    logger.info(
        "dev_inline_dispatched",
        extra={"segment_count": len(segments)},
    )
    return f"dev-inline-task/{key}"
