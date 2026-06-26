"""Local-dev inline replacement for Cloud Tasks dispatch.

Activated by ``Settings.DEV_INLINE_NAME_RESOLUTION``. The signature of
:func:`run_inline_name_resolution` matches
:func:`app.tasks_dispatch.enqueue_name_resolution` so :mod:`app.main`
can swap one for the other with a single branch.

Instead of enqueueing to Cloud Tasks, we spawn a daemon thread that runs
OPSIN → RDKit → Firestore → ``response_url`` POST inline. The thread is
fire-and-forget; failures are logged but never raised because there is
no Cloud Tasks queue to retry against in dev.

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
from typing import Any

from . import slack_dispatch
from .config import Settings
from .opsin_utils import iupac_to_smiles
from .rdkit_utils import MoleculeGenerationError


logger = logging.getLogger("molcast.dev_dispatch")


def _idempotency_key(response_url: str) -> str:
    return hashlib.sha256(response_url.encode("utf-8")).hexdigest()[:32]


def _build_error_payload(text: str, *, settings: Settings) -> dict[str, Any]:
    return {
        "response_type": settings.SLACK_RESPONSE_TYPE,
        "replace_original": False,
        "text": text,
    }


def _resolve_and_post(
    *,
    name: str,
    response_url: str,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    settings: Settings,
    flags: dict[str, bool] | None = None,
) -> None:
    """Thread target. Mirrors the OIDC-less subset of
    :func:`app.main.internal_process` — OPSIN → SMILES → RDKit →
    Firestore → ``response_url`` POST. All exceptions are caught here
    because the caller has already ack'd Slack and there is nothing
    above this thread to receive a raised error.
    """
    from .main import _process_smiles_sync  # late import: avoids cycle

    logger.info(
        "dev_inline_started",
        extra={"input_name_len": len(name)},
    )
    try:
        smiles = iupac_to_smiles(name, backend=settings.OPSIN_BACKEND)
    except MoleculeGenerationError as exc:
        logger.info(
            "dev_inline_user_error",
            extra={
                "error_kind": "MoleculeGenerationError",
                "input_name_len": len(name),
            },
        )
        slack_dispatch.post_to_response_url(
            response_url,
            _build_error_payload(str(exc), settings=settings),
        )
        return
    except Exception:
        logger.exception("dev_inline_opsin_unexpected_error")
        slack_dispatch.post_to_response_url(
            response_url,
            _build_error_payload(
                "OPSIN 呼び出しでエラーが発生しました。", settings=settings
            ),
        )
        return

    payload = _process_smiles_sync(
        smiles=smiles,
        input_name=name,
        user_id=user_id,
        channel_id=channel_id,
        base_url=base_url,
        settings=settings,
        flags=flags,
    )
    slack_dispatch.post_to_response_url(response_url, payload)
    logger.info(
        "dev_inline_completed",
        extra={"input_name_len": len(name), "smiles_len": len(smiles)},
    )


def run_inline_name_resolution(
    *,
    name: str,
    response_url: str,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    settings: Settings,
    flags: dict[str, bool] | None = None,
) -> str:
    """Spawn the resolution thread and return a synthetic ``task_name``.

    The return value is logged by the caller for parity with the Cloud
    Tasks code path; it carries no semantic meaning in dev.
    """
    key = _idempotency_key(response_url) if response_url else "no-response-url"
    thread = threading.Thread(
        target=_resolve_and_post,
        kwargs={
            "name": name,
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
        extra={"input_name_len": len(name)},
    )
    return f"dev-inline-task/{key}"
