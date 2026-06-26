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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import dev_dispatch, slack_dispatch, store, tasks_dispatch, templates
from .config import Settings, get_settings
from .logging_config import configure_logging
from .oidc_verify import OIDCVerificationError, verify_oidc_token
from .opsin_utils import iupac_to_smiles
from .rdkit_utils import (
    MoleculeGenerationError,
    generate_3d_molblock,
    molblock_to_formula_and_weight,
    molblock_to_svg,
)
from .slack_verify import verify_slack_request
from .tasks_dispatch import TasksConfigError

logger = logging.getLogger("molcast.main")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(level=settings.LOG_LEVEL)
    logger.info("startup", extra={"backend": settings.OPSIN_BACKEND})
    # Surface dev-only bypasses at WARNING so a misconfigured prod deploy
    # is obvious from a single log line. Both flags default False; either
    # being True is intentional in local dev only.
    if (
        settings.DEV_SKIP_SIGNATURE_VERIFICATION
        or settings.DEV_INLINE_NAME_RESOLUTION
    ):
        logger.warning(
            "dev_flags_active",
            extra={
                "skip_signature": settings.DEV_SKIP_SIGNATURE_VERIFICATION,
                "inline_name": settings.DEV_INLINE_NAME_RESOLUTION,
            },
        )
    # Cold-start warm-up (#8). The first /mol CCO after a fresh instance
    # spin-up pays the cost of importing rdkit.Chem.AllChem (heavy
    # Boost::Python module) AND warming RDKit's internal force-field
    # parameter cache. Doing one dummy embed here pulls both costs out
    # of the user-visible request path. Failure is logged and swallowed
    # — startup must not crash because of a warm-up issue.
    try:
        generate_3d_molblock("CCO", max_atoms=settings.MAX_ATOMS)
        logger.info("warmup_ok")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "warmup_failed",
            extra={"error_kind": type(exc).__name__},
        )
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


# `/mol` flag parsing (#3) -------------------------------------------------
# Known flags (whitespace-delimited tokens stripped from the input before
# classification). Order-independent. Unknown ``--flag`` tokens are left in
# place — they fall through to SMILES classification and OPSIN will reject
# them with the standard user error, which surfaces the typo.
_FLAG_PUBLIC = "--public"
_FLAG_LABEL = "--label"
_FLAG_NO_3D = "--no-3d"
_KNOWN_FLAGS = frozenset({_FLAG_PUBLIC, _FLAG_LABEL, _FLAG_NO_3D})


def _extract_flags(text: str) -> tuple[dict[str, bool], str]:
    """Return ``(flags, cleaned_text)`` with known ``--flag`` tokens removed.

    Flags are matched as whole whitespace-separated tokens so they cannot
    accidentally chew into a SMILES (SMILES never contains whitespace).
    """
    flags = {"public": False, "label": False, "no_3d": False}
    tokens = (text or "").split()
    kept: list[str] = []
    for tok in tokens:
        if tok == _FLAG_PUBLIC:
            flags["public"] = True
        elif tok == _FLAG_LABEL:
            flags["label"] = True
        elif tok == _FLAG_NO_3D:
            flags["no_3d"] = True
        else:
            kept.append(tok)
    # Preserve a leading "name:" attached to the next token (Slack splits
    # on whitespace just like we do, so "name: ethanol" arrives as two
    # tokens "name:" and "ethanol" — re-joining with a single space
    # is what _classify_text expects).
    return (flags, " ".join(kept))


def _viewer_url_with_flags(
    base_url: str, mol_id: str, flags: dict[str, bool]
) -> str:
    """Append ``?label=1`` / ``?mode=2d`` query params from the flag dict."""
    params: list[str] = []
    if flags.get("label"):
        params.append("label=1")
    if flags.get("no_3d"):
        params.append("mode=2d")
    url = f"{base_url}/view/{mol_id}"
    if params:
        url += "?" + "&".join(params)
    return url


def _process_smiles_sync(
    *,
    smiles: str,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    settings: Settings,
    input_name: str | None = None,
    flags: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """SMILES -> RDKit -> Firestore -> Slack response payload.

    Synchronous: every branch returns a dict suitable for direct
    inclusion in the HTTP response body (Slack interprets this exactly
    like a ``response_url`` POST). The caller wraps the returned dict
    in ``JSONResponse``.

    ``input_name`` is the original ``/mol name: ...`` input (Phase 3); it
    is persisted to Firestore but the rest of the pipeline operates on
    the resolved SMILES. ``None`` for the SMILES route.

    ``flags`` is the parsed flag dict from :func:`_extract_flags` (#3).
    Recognised keys: ``public`` (in_channel response_type), ``label``
    (query param on viewer URL), ``no_3d`` (route viewer to 2D mode).
    ``None`` is treated as all-False.
    """
    started = time.monotonic()
    flags = flags or {"public": False, "label": False, "no_3d": False}
    # --public overrides the configured response scope so the final
    # message lands in_channel even when the deploy default is
    # ephemeral. The error branches below use the configured default so
    # rdkit errors don't get loudly posted to the channel.
    success_response_type = (
        "in_channel" if flags.get("public") else settings.SLACK_RESPONSE_TYPE
    )
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
            input_name=input_name,
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

    viewer_url = _viewer_url_with_flags(base_url, mol_id, flags)
    log_extra: dict[str, Any] = {
        "mol_id": mol_id,
        "created_by": user_id,
        "channel_id": channel_id,
        "smiles_len": len(smiles),
        "molblock_len": len(molblock),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "flag_public": bool(flags.get("public")),
        "flag_label": bool(flags.get("label")),
        "flag_no_3d": bool(flags.get("no_3d")),
    }
    if input_name is not None:
        log_extra["input_name_len"] = len(input_name)
    logger.info("mol_created", extra=log_extra)

    # 完了文に何を生成したかを 1 行添える (#1)。
    # name: 経路は `<入力名>` -> `<SMILES>` の対応を見せる。
    # SMILES 経路は SMILES だけ載せる。Slack のバッククォートでコード扱い
    # にして長い文字列でも改行が走らないようにする。
    if input_name:
        provenance = f"`{input_name}` → `{smiles}`"
    else:
        provenance = f"SMILES: `{smiles}`"
    heading = "2D 構造ビューア" if flags.get("no_3d") else "3D ビューア"
    return {
        "response_type": success_response_type,
        "replace_original": False,
        "text": f"{heading}を生成しました: {viewer_url}\n{provenance}",
    }


@app.post("/slack/mol")
async def slack_mol(request: Request) -> JSONResponse:
    settings = get_settings()
    body = await request.body()

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if settings.DEV_SKIP_SIGNATURE_VERIFICATION:
        # Local-dev: signature verification is bypassed. The startup
        # WARNING log surfaces this state; per-request logging would be
        # too noisy.
        pass
    elif not verify_slack_request(
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

    # Pull flags first so the residual text classifies cleanly.
    flags, residual_text = _extract_flags(text)
    kind, payload = _classify_text(residual_text)

    if kind == "empty":
        return JSONResponse(
            _ephemeral(
                "使い方: `/mol <SMILES>` または `/mol name: <IUPAC 名>`\n"
                "オプション: `--public` (チャンネルに投稿) / "
                "`--label` (原子ラベル表示) / `--no-3d` (2D 描画のみ)\n"
                f"座標ファイルを描画するには {base_url}/view/ を開いてドラッグ&ドロップしてください。"
            )
        )

    if kind == "name":
        # name: route is two-stage. We must ack within Slack's 3 s
        # window; OPSIN (subprocess + JVM ~1-2 s) and RDKit add up
        # to >3 s on cold start, so the heavy work is shifted to
        # Cloud Tasks. The response_url Slack hands us in the form
        # data is the channel for the eventual viewer URL.
        response_url = form.get("response_url", "")
        if not response_url:
            return JSONResponse(
                _ephemeral(
                    "Slack の response_url が取得できませんでした。再度お試しください。",
                    response_type=settings.SLACK_RESPONSE_TYPE,
                )
            )
        try:
            if settings.DEV_INLINE_NAME_RESOLUTION:
                # Local-dev: run OPSIN + RDKit + Firestore + Slack POST
                # inline (in a daemon thread) instead of enqueueing to
                # Cloud Tasks. Returns immediately so the 3 s ack
                # window is not stressed.
                task_name = dev_dispatch.run_inline_name_resolution(
                    name=payload,
                    response_url=response_url,
                    user_id=user_id,
                    channel_id=channel_id,
                    base_url=base_url,
                    settings=settings,
                    flags=flags,
                )
            else:
                task_name = tasks_dispatch.enqueue_name_resolution(
                    name=payload,
                    response_url=response_url,
                    user_id=user_id,
                    channel_id=channel_id,
                    base_url=base_url,
                    settings=settings,
                    flags=flags,
                )
        except TasksConfigError as exc:
            logger.exception(
                "tasks_config_error",
                extra={"error_kind": type(exc).__name__},
            )
            return JSONResponse(
                _ephemeral(
                    "サーバ側の設定が未完了です。管理者にお知らせください。",
                    response_type=settings.SLACK_RESPONSE_TYPE,
                )
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("tasks_enqueue_failed")
            return JSONResponse(
                _ephemeral(
                    "ジョブの送出に失敗しました。しばらく待ってから再度お試しください。",
                    response_type=settings.SLACK_RESPONSE_TYPE,
                )
            )
        logger.info(
            "tasks_dispatched",
            extra={
                "input_name_len": len(payload),
                "created_by": user_id,
                "channel_id": channel_id,
            },
        )
        del task_name
        # 何を解決中かを ack に出すと、複数同時実行時にどれが自分のリクエスト
        # か追えるようになる。"処理中" を残してあるのは test_main.py の
        # 既存アサーション (#1) と運用的な検索性のため。
        return JSONResponse(
            _ephemeral(
                f"`{payload}` を処理中です。完了したら結果を投稿します。",
                response_type=settings.SLACK_RESPONSE_TYPE,
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
            flags=flags,
        )
    )


# ---------------------------------------------------------------------------
# Cloud Tasks worker endpoint (name: route, two-stage flow)
# ---------------------------------------------------------------------------
@app.post("/internal/process")
async def internal_process(request: Request) -> Response:
    """Worker for the name: route. Cloud Tasks dispatches POSTs here
    with an OIDC bearer token; we verify it, claim an idempotency key
    in Firestore, run OPSIN + RDKit + Firestore, and POST the result
    to Slack's ``response_url``.

    Response codes drive Cloud Tasks' retry decision:

      * 204 — done (success or user error already reported to Slack).
              CT will NOT retry.
      * 403 — OIDC verification failed. CT will NOT retry.
      * 500 — transient infrastructure failure (Firestore down, etc).
              CT will retry per the queue's backoff config.

    A 204 with a Slack message is the right shape for a user error
    (bad name, unparseable structure) — the user has the answer,
    retrying would be pointless.
    """
    settings = get_settings()

    expected_audience = settings.BASE_URL or _base_url(request, settings)
    try:
        verify_oidc_token(
            request.headers.get("Authorization"),
            expected_audience=expected_audience,
            expected_principal=settings.TASKS_INVOKER_SA,
        )
    except OIDCVerificationError:
        return PlainTextResponse("forbidden", status_code=403)

    try:
        body = await request.json()
    except Exception:
        logger.warning("internal_process_bad_body")
        return PlainTextResponse("bad request", status_code=400)

    if body.get("schema_version") != 1 or body.get("kind") != "name":
        logger.warning(
            "internal_process_bad_schema",
            extra={"error_kind": "BadSchema"},
        )
        return PlainTextResponse("bad request", status_code=400)

    name: str = body.get("payload") or ""
    response_url: str = body.get("response_url") or ""
    user_id: str | None = body.get("user_id")
    channel_id: str | None = body.get("channel_id")
    base_url: str = body.get("base_url") or ""
    idempotency_key: str = body.get("idempotency_key") or ""
    # Flags propagated from the slash command (#3). Optional in the
    # schema so old tasks in flight before the upgrade still parse.
    raw_flags = body.get("flags") or {}
    flags = {
        "public": bool(raw_flags.get("public")),
        "label": bool(raw_flags.get("label")),
        "no_3d": bool(raw_flags.get("no_3d")),
    }
    if not name or not response_url or not base_url or not idempotency_key:
        return PlainTextResponse("bad request", status_code=400)

    # ----- Idempotency guard --------------------------------------------------
    try:
        claimed = store.claim_idempotency_key(
            idempotency_key,
            collection=settings.IDEMPOTENCY_COLLECTION,
            ttl_seconds=settings.IDEMPOTENCY_TTL_SECONDS,
        )
    except Exception:
        logger.exception("idempotency_claim_failed")
        # Transient infra failure → let CT retry.
        return PlainTextResponse("retry", status_code=500)
    if not claimed:
        logger.info(
            "internal_process_duplicate",
            extra={"error_kind": "DuplicateDispatch"},
        )
        return PlainTextResponse("", status_code=204)

    # ----- OPSIN resolution ---------------------------------------------------
    opsin_started = time.monotonic()
    try:
        resolved_smiles = iupac_to_smiles(name, backend=settings.OPSIN_BACKEND)
    except MoleculeGenerationError as exc:
        logger.info(
            "opsin_user_error",
            extra={
                "error_kind": "MoleculeGenerationError",
                "backend": settings.OPSIN_BACKEND,
                "input_name_len": len(name),
                "elapsed_ms": int((time.monotonic() - opsin_started) * 1000),
            },
        )
        slack_dispatch.post_to_response_url(
            response_url,
            _ephemeral(str(exc), response_type=settings.SLACK_RESPONSE_TYPE),
        )
        return PlainTextResponse("", status_code=204)
    logger.info(
        "opsin_resolved",
        extra={
            "backend": settings.OPSIN_BACKEND,
            "input_name_len": len(name),
            "smiles_len": len(resolved_smiles),
            "elapsed_ms": int((time.monotonic() - opsin_started) * 1000),
        },
    )

    # ----- RDKit + Firestore (re-use existing pipeline) -----------------------
    payload = _process_smiles_sync(
        smiles=resolved_smiles,
        input_name=name,
        user_id=user_id,
        channel_id=channel_id,
        base_url=base_url,
        settings=settings,
        flags=flags,
    )
    slack_dispatch.post_to_response_url(response_url, payload)
    return PlainTextResponse("", status_code=204)


# ---------------------------------------------------------------------------
# Viewer endpoints
# ---------------------------------------------------------------------------
@app.get("/view/{mol_id}", response_class=HTMLResponse)
def view_mol(mol_id: str, request: Request) -> HTMLResponse:
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

    smiles = data.get("smiles")
    molblock = data.get("molblock")
    # Compute formula / MW once per request (#6). RDKit failure leaves
    # both as None — the template handles that gracefully.
    formula, mol_weight = molblock_to_formula_and_weight(molblock or "")

    # ``?mode=2d`` (from /mol --no-3d, or hand-typed) renders the static
    # SVG page instead of 3Dmol.js (#3).
    if request.query_params.get("mode") == "2d":
        svg_text = molblock_to_svg(molblock or "")
        return HTMLResponse(
            templates.render_viewer_2d_html(
                smiles=smiles,
                svg_text=svg_text,
                mol_id=mol_id,
                formula=formula,
                mol_weight=mol_weight,
            )
        )

    return HTMLResponse(
        templates.render_viewer_html(
            smiles=smiles,
            molblock=molblock,
            mol_id=mol_id,
            formula=formula,
            mol_weight=mol_weight,
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
