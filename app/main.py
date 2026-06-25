"""FastAPI entrypoint (see §5, §8 of the design brief).

Endpoint summary:

  GET  /health         -> liveness check.
  POST /slack/mol      -> Slack slash command; verify signature, classify
                          ``text``, run RDKit + Firestore synchronously,
                          return the viewer URL (or an error message)
                          directly in the response body.
  GET  /view/{mol_id}  -> render 3Dmol.js viewer with stored MolBlock.
  GET  /view/, GET /   -> bare viewer (drop-zone for coordinate files).

Phase 1 deviates from the brief's §5.2 ack-then-response_url two-stage
flow for one reason: Cloud Run's default ``cpu-throttling=always`` puts
the FastAPI ``BackgroundTasks`` body into a near-stalled state after
the ack response is returned, and the alternative (``--no-cpu-
throttling``) carries continuous CPU billing. A synchronous response
keeps cpu-throttling at its default (zero-cost-when-idle) AND fits
within Slack's 3-second ack window for the lightweight molecules the
lab actually uses; the cold-start case may occasionally exceed 3 s,
in which case the user simply re-issues the command on a warm
instance. The ``slack_dispatch`` module is retained because Phase 3
(OPSIN with JVM cold start) may push past 3 s and require the
two-stage flow there.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import store, templates
from .config import Settings, get_settings
from .logging_config import configure_logging
from .rdkit_utils import MoleculeGenerationError, generate_3d_molblock
from .slack_verify import verify_slack_request

logger = logging.getLogger("molcast.main")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(level=settings.LOG_LEVEL)
    logger.info("startup", extra={"backend": settings.OPSIN_BACKEND})
    yield


app = FastAPI(title="Molcast Viewer", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Slack slash command
# ---------------------------------------------------------------------------
def _base_url(request: Request, settings: Settings) -> str:
    """BASE_URL override wins; otherwise reconstruct from the request."""
    if settings.BASE_URL:
        return settings.BASE_URL.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def _ephemeral(text: str, *, response_type: str | None = None) -> dict[str, Any]:
    # ``replace_original: False`` is Slack's documented default, but §5.2
    # of the brief shows it in both the success and failure example
    # payloads, so we emit it everywhere for shape consistency.
    return {
        "response_type": response_type or "ephemeral",
        "replace_original": False,
        "text": text,
    }


def _classify_text(text: str) -> tuple[str, str]:
    """Return (kind, payload).

    kind in {'empty', 'name', 'smiles'}; payload is the cleaned input.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ("empty", "")
    if cleaned.lower().startswith("name:"):
        return ("name", cleaned[len("name:") :].strip())
    return ("smiles", cleaned)


def _process_smiles_sync(
    *,
    smiles: str,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    settings: Settings,
) -> dict[str, Any]:
    """SMILES -> RDKit -> Firestore -> Slack response payload.

    Synchronous: every branch returns a dict suitable for direct
    inclusion in the HTTP response body (Slack interprets this exactly
    like a ``response_url`` POST). The caller wraps the returned dict
    in ``JSONResponse``.
    """
    started = time.monotonic()
    try:
        molblock = generate_3d_molblock(smiles, max_atoms=settings.MAX_ATOMS)
    except MoleculeGenerationError as exc:
        logger.info(
            "rdkit_user_error",
            extra={
                "error_kind": "MoleculeGenerationError",
                "smiles_len": len(smiles),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return _ephemeral(str(exc), response_type=settings.SLACK_RESPONSE_TYPE)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "rdkit_unexpected_error",
            extra={"smiles_len": len(smiles)},
        )
        return _ephemeral(
            "予期しないエラーが発生しました。SMILES をご確認ください。",
            response_type=settings.SLACK_RESPONSE_TYPE,
        )

    mol_id = store.new_mol_id()
    try:
        store.save_molecule(
            mol_id=mol_id,
            smiles=smiles,
            molblock=molblock,
            input_name=None,
            created_by=user_id,
            channel_id=channel_id,
            collection=settings.FIRESTORE_COLLECTION,
            retention_days=settings.RETENTION_DAYS,
        )
    except Exception:
        logger.exception(
            "firestore_save_failed",
            extra={"mol_id": mol_id, "molblock_len": len(molblock)},
        )
        return _ephemeral(
            "保存に失敗しました。しばらく待ってから再度お試しください。",
            response_type=settings.SLACK_RESPONSE_TYPE,
        )

    viewer_url = f"{base_url}/view/{mol_id}"
    logger.info(
        "mol_created",
        extra={
            "mol_id": mol_id,
            "created_by": user_id,
            "channel_id": channel_id,
            "smiles_len": len(smiles),
            "molblock_len": len(molblock),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return {
        "response_type": settings.SLACK_RESPONSE_TYPE,
        "replace_original": False,
        "text": f"3D viewer generated: {viewer_url}",
    }


@app.post("/slack/mol")
async def slack_mol(request: Request) -> JSONResponse:
    settings = get_settings()
    body = await request.body()

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_request(
        settings.SLACK_SIGNING_SECRET, timestamp, signature, body
    ):
        # Do not echo the body or the offending header — just refuse.
        raise HTTPException(status_code=403, detail="signature verification failed")

    # FastAPI's Form parsing would re-read the body, but we've already
    # consumed it. Re-parse the URL-encoded body manually.
    from urllib.parse import parse_qs

    form_raw = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    form = {k: v[0] for k, v in form_raw.items() if v}

    text = form.get("text", "")
    user_id = form.get("user_id")
    channel_id = form.get("channel_id")

    base_url = _base_url(request, settings)

    kind, payload = _classify_text(text)

    if kind == "empty":
        return JSONResponse(
            _ephemeral(
                "使い方: `/mol <SMILES>` または `/mol name: <IUPAC 名>`\n"
                f"座標ファイルを描画するには {base_url}/view/ を開いてドラッグ&ドロップしてください。"
            )
        )

    if kind == "name":
        # Phase 3 で OPSIN を有効化する。Phase 1 では即時に「未対応」を返す。
        return JSONResponse(
            _ephemeral(
                "`name:` 経路は Phase 3 で対応予定です。"
                "現在は SMILES を直接入力してください: `/mol <SMILES>`"
            )
        )

    # SMILES path — runs synchronously and returns the viewer URL or an
    # error message directly. No response_url callback needed.
    return JSONResponse(
        _process_smiles_sync(
            smiles=payload,
            user_id=user_id,
            channel_id=channel_id,
            base_url=base_url,
            settings=settings,
        )
    )


# ---------------------------------------------------------------------------
# Viewer endpoints
# ---------------------------------------------------------------------------
@app.get("/view/{mol_id}", response_class=HTMLResponse)
def view_mol(mol_id: str) -> HTMLResponse:
    settings = get_settings()
    try:
        data = store.get_molecule(mol_id, collection=settings.FIRESTORE_COLLECTION)
    except Exception:
        logger.exception("firestore_get_failed", extra={"mol_id": mol_id})
        return HTMLResponse(
            templates.render_not_found_html(), status_code=503
        )

    if data is None:
        return HTMLResponse(templates.render_not_found_html(), status_code=404)
    if data.get("expired"):
        return HTMLResponse(
            templates.render_expired_html(settings.RETENTION_DAYS),
            status_code=410,
        )
    return HTMLResponse(
        templates.render_viewer_html(
            smiles=data.get("smiles"),
            molblock=data.get("molblock"),
            mol_id=mol_id,
        )
    )


@app.get("/view/", response_class=HTMLResponse)
@app.get("/view", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def view_bare() -> HTMLResponse:
    return HTMLResponse(
        templates.render_viewer_html(smiles=None, molblock=None, mol_id=None)
    )


# Silence the noisy 404 for /favicon.ico without serving anything.
@app.get("/favicon.ico")
def favicon() -> PlainTextResponse:
    return PlainTextResponse("", status_code=204)
