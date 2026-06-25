"""FastAPI entrypoint (see §5, §8 of the design brief).

Endpoint summary:

  GET  /health         -> liveness check.
  POST /slack/mol      -> Slack slash command; verify signature, classify
                          ``text``, dispatch a BackgroundTask, return ack.
  GET  /view/{mol_id}  -> render 3Dmol.js viewer with stored MolBlock.
  GET  /view/, GET /   -> bare viewer (drop-zone for coordinate files).

The slash command flow is always two-stage (sync ack + async result via
``response_url``), even when the work would fit in 3 seconds. §5.2 calls
this out explicitly: a single code path is easier to reason about than
a sometimes-sync / sometimes-async hybrid.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import store, templates
from .config import Settings, get_settings
from .logging_config import configure_logging
from .rdkit_utils import MoleculeGenerationError, generate_3d_molblock
from .slack_dispatch import post_to_response_url
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


def _process_smiles_job(
    *,
    smiles: str,
    response_url: str,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    settings: Settings,
) -> None:
    """BackgroundTask body: SMILES -> RDKit -> Firestore -> response_url."""
    started = time.monotonic()
    mol_id = store.new_mol_id()
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
        post_to_response_url(
            response_url,
            _ephemeral(str(exc), response_type=settings.SLACK_RESPONSE_TYPE),
        )
        return
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "rdkit_unexpected_error",
            extra={"smiles_len": len(smiles)},
        )
        post_to_response_url(
            response_url,
            _ephemeral(
                "予期しないエラーが発生しました。SMILES をご確認ください。",
                response_type=settings.SLACK_RESPONSE_TYPE,
            ),
        )
        return

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
        post_to_response_url(
            response_url,
            _ephemeral(
                "保存に失敗しました。しばらく待ってから再度お試しください。",
                response_type=settings.SLACK_RESPONSE_TYPE,
            ),
        )
        return

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
    post_to_response_url(
        response_url,
        _ephemeral(
            f"3D viewer generated: {viewer_url}",
            response_type=settings.SLACK_RESPONSE_TYPE,
        ),
    )


@app.post("/slack/mol")
async def slack_mol(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
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
    response_url = form.get("response_url", "")
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

    if not response_url:
        # Slack from a real workspace always sends response_url; defensive
        # so a misconfigured tester does not silently lose the job.
        return JSONResponse(
            _ephemeral(
                "response_url が見つかりません。Slack App の設定をご確認ください。"
            )
        )

    background_tasks.add_task(
        _process_smiles_job,
        smiles=payload,
        response_url=response_url,
        user_id=user_id,
        channel_id=channel_id,
        base_url=base_url,
        settings=settings,
    )
    return JSONResponse(
        _ephemeral("処理中です... 完了したら結果をこのスレッドに投稿します。")
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
