"""FastAPI entrypoint (see §5, §8 of the design brief).

Endpoint summary:

  GET  /health         -> liveness check.
  POST /slack/mol      -> Slack slash command; verify signature, split
                          ``text`` into ``;``-separated segments, run
                          RDKit + Firestore synchronously (SMILES-only)
                          or hand the whole trajectory to Cloud Tasks
                          when any segment is ``name:`` based.
  POST /internal/process -> Cloud Tasks worker (OIDC-authenticated).
                          Resolves each ``name:`` segment via OPSIN,
                          runs RDKit, saves the trajectory document,
                          POSTs the viewer URL to Slack's ``response_url``.
  GET  /view/{mol_id}  -> render 3Dmol.js viewer (or 2D SVG with
                          ``?mode=2d``) for a stored trajectory.
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

Trajectory mode (current): ``/mol CCO ; CC(C)O ; name: DMSO`` builds a
single Firestore document whose ``frames`` array holds one entry per
segment; the viewer ships a navigation strip to step through them. A
single-segment input degenerates to the same shape (``len(frames)==1``)
so the storage / rendering paths have no branching for "single vs
multi".
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


# Defensive cap on trajectory length. Slack slash-command text is
# limited to a few thousand chars upstream; this cap is the *server*
# safety net that bounds Firestore document size and avoids pathological
# /mol invocations. 20 frames * ~10 KB MolBlock = ~200 KB, well under
# Firestore's 1 MB doc limit and small enough that the viewer page
# stays snappy.
MAX_TRAJECTORY_FRAMES = 20


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
# Slack slash command helpers
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
    """Return (kind, payload) for a single (already split) segment.

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
    Flags are stripped BEFORE ``;`` splitting so the same flag set
    applies to every segment of the trajectory.
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
    return (flags, " ".join(kept))


def _split_segments(
    text: str, *, cap: int = MAX_TRAJECTORY_FRAMES
) -> tuple[list[tuple[str, str]], bool]:
    """Split flag-stripped text on ``;`` into classified segments.

    Returns ``(segments, was_capped)``. ``segments`` is a list of
    ``(kind, payload)`` tuples; empty segments are dropped silently
    (so ``A ; ; B`` parses as two segments, not three). ``was_capped``
    is True iff the input had more than ``cap`` non-empty segments —
    the caller is expected to surface this to the user.
    """
    if not text:
        return ([], False)
    raw_parts = [p.strip() for p in text.split(";")]
    out: list[tuple[str, str]] = []
    overflow = False
    for part in raw_parts:
        if not part:
            continue
        kind, payload = _classify_text(part)
        if kind == "empty":
            continue
        if len(out) >= cap:
            overflow = True
            break
        out.append((kind, payload))
    return (out, overflow)


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


# ---------------------------------------------------------------------------
# Frame pipeline (shared by sync SMILES path and Cloud Tasks worker)
# ---------------------------------------------------------------------------
def _resolve_segment_to_frame(
    kind: str, payload: str, *, settings: Settings
) -> dict[str, Any]:
    """Run one segment through name→SMILES→MolBlock.

    Returns a frame dict ready for :func:`store.save_trajectory`. A
    failure at any stage populates ``error`` and leaves the unfilled
    downstream fields as ``None`` — the viewer renders an error overlay
    for such frames so the trajectory order is preserved.
    """
    frame: dict[str, Any] = {
        "kind": kind, "input": payload,
        "smiles": None, "molblock": None, "error": None,
    }
    if kind == "name":
        try:
            frame["smiles"] = iupac_to_smiles(
                payload, backend=settings.OPSIN_BACKEND
            )
        except MoleculeGenerationError as exc:
            frame["error"] = str(exc)
            return frame
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("opsin_unexpected_error")
            frame["error"] = f"OPSIN 呼び出しでエラー: {type(exc).__name__}"
            return frame
    else:
        frame["smiles"] = payload
    try:
        frame["molblock"] = generate_3d_molblock(
            frame["smiles"], max_atoms=settings.MAX_ATOMS
        )
    except MoleculeGenerationError as exc:
        frame["error"] = str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("rdkit_unexpected_error")
        frame["error"] = f"RDKit エラー: {type(exc).__name__}"
    return frame


def _build_slack_response_text(
    *, frames: list[dict[str, Any]], viewer_url: str,
    flags: dict[str, bool], was_capped: bool,
) -> str:
    """Slack message body. Single-frame keeps the old shape; N>1 lists a
    short summary of segments + a count line."""
    n_ok = sum(1 for f in frames if f.get("molblock"))
    n_total = len(frames)
    heading_kind = "2D" if flags.get("no_3d") else "3D"
    if n_total == 1:
        f = frames[0]
        if f.get("error"):
            # Single failed frame — surface the error in place of the
            # viewer URL since the viewer would just show an error page.
            return f["error"]
        if f.get("kind") == "name":
            provenance = f"`{f['input']}` → `{f['smiles']}`"
        else:
            provenance = f"SMILES: `{f['smiles']}`"
        heading = "2D 構造ビューア" if flags.get("no_3d") else "3D ビューア"
        return f"{heading}を生成しました: {viewer_url}\n{provenance}"
    # Multi-frame summary.
    lines = [
        f"{heading_kind} トラジェクトリ ({n_ok}/{n_total} 構造) を生成しました: "
        f"{viewer_url}"
    ]
    max_inline = 5
    for i, f in enumerate(frames[:max_inline], start=1):
        status = "✗" if f.get("error") else "✓"
        lines.append(f"  [{i}] `{f.get('input', '?')}` {status}")
    if n_total > max_inline:
        lines.append(f"  ...他 {n_total - max_inline} フレーム")
    if was_capped:
        lines.append(
            f"  ⚠ 上限 {MAX_TRAJECTORY_FRAMES} フレームを超えたため、"
            "残りは破棄しました。"
        )
    return "\n".join(lines)


def process_and_save_frames(
    *,
    segments: list[tuple[str, str]],
    was_capped: bool,
    user_id: str | None,
    channel_id: str | None,
    base_url: str,
    settings: Settings,
    flags: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Resolve each segment, persist as one trajectory doc, build Slack reply.

    Called from the sync SMILES path in :func:`slack_mol` AND from the
    Cloud Tasks worker in :func:`internal_process`. Per-segment errors
    do not abort the trajectory — they ride as ``frame.error`` so the
    viewer can show "what worked, what didn't" rather than silently
    dropping failed segments.
    """
    started = time.monotonic()
    flags = flags or {"public": False, "label": False, "no_3d": False}
    success_response_type = (
        "in_channel" if flags.get("public") else settings.SLACK_RESPONSE_TYPE
    )

    if not segments:
        return _ephemeral(
            "解析対象が空です。`/mol <SMILES>` または `/mol name: <IUPAC 名>` を指定してください。",
            response_type=settings.SLACK_RESPONSE_TYPE,
        )

    frames = [
        _resolve_segment_to_frame(kind, payload, settings=settings)
        for kind, payload in segments
    ]

    if not any(f.get("molblock") for f in frames):
        # All segments failed; don't bother persisting. Surface the
        # first error verbatim so the user can fix the input.
        first_err = next(
            (f["error"] for f in frames if f.get("error")),
            "全フレームの生成に失敗しました。",
        )
        logger.info(
            "trajectory_all_failed",
            extra={"frame_count": len(frames)},
        )
        return _ephemeral(first_err, response_type=settings.SLACK_RESPONSE_TYPE)

    mol_id = store.new_mol_id()
    try:
        store.save_trajectory(
            mol_id=mol_id,
            frames=frames,
            flags=flags,
            created_by=user_id,
            channel_id=channel_id,
            collection=settings.FIRESTORE_COLLECTION,
            retention_days=settings.RETENTION_DAYS,
        )
    except Exception:
        logger.exception(
            "firestore_save_failed",
            extra={"mol_id": mol_id, "frame_count": len(frames)},
        )
        return _ephemeral(
            "保存に失敗しました。しばらく待ってから再度お試しください。",
            response_type=settings.SLACK_RESPONSE_TYPE,
        )

    viewer_url = _viewer_url_with_flags(base_url, mol_id, flags)
    logger.info(
        "trajectory_created",
        extra={
            "mol_id": mol_id,
            "frame_count": len(frames),
            "frame_ok": sum(1 for f in frames if f.get("molblock")),
            "frame_err": sum(1 for f in frames if f.get("error")),
            "was_capped": was_capped,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "flag_public": bool(flags.get("public")),
            "flag_label": bool(flags.get("label")),
            "flag_no_3d": bool(flags.get("no_3d")),
            "created_by": user_id,
            "channel_id": channel_id,
        },
    )
    return {
        "response_type": success_response_type,
        "replace_original": False,
        "text": _build_slack_response_text(
            frames=frames, viewer_url=viewer_url,
            flags=flags, was_capped=was_capped,
        ),
    }


# ---------------------------------------------------------------------------
# /slack/mol
# ---------------------------------------------------------------------------
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

    # Flags first (apply to the whole trajectory), then split on ``;``.
    flags, residual_text = _extract_flags(text)
    segments, was_capped = _split_segments(residual_text)

    if not segments:
        return JSONResponse(
            _ephemeral(
                "使い方: `/mol <SMILES>` または `/mol name: <IUPAC 名>`\n"
                "複数構造をまとめて生成するには `;` で区切る: "
                "`/mol CCO ; CC(C)O ; name: DMSO`\n"
                "オプション: `--public` (チャンネルに投稿) / "
                "`--label` (原子ラベル表示) / `--no-3d` (2D 描画のみ)\n"
                f"座標ファイルを描画するには {base_url}/view/ を開いてドラッグ&ドロップしてください。"
            )
        )

    has_name = any(kind == "name" for kind, _ in segments)
    if has_name:
        # Two-stage: ack within 3 s, hand the whole trajectory (every
        # segment, name AND smiles) to the worker so OPSIN cold start
        # doesn't push us past Slack's window.
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
                task_name = dev_dispatch.run_inline_name_resolution(
                    segments=segments,
                    was_capped=was_capped,
                    response_url=response_url,
                    user_id=user_id,
                    channel_id=channel_id,
                    base_url=base_url,
                    settings=settings,
                    flags=flags,
                )
            else:
                task_name = tasks_dispatch.enqueue_name_resolution(
                    segments=segments,
                    was_capped=was_capped,
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
                "segment_count": len(segments),
                "created_by": user_id,
                "channel_id": channel_id,
            },
        )
        del task_name
        # Ack message echoes the inputs so a user with multiple in-flight
        # invocations can tell which is which.
        ack_preview = _segments_preview(segments)
        return JSONResponse(
            _ephemeral(
                f"`{ack_preview}` を処理中です。完了したら結果を投稿します。",
                response_type=settings.SLACK_RESPONSE_TYPE,
            )
        )

    # SMILES-only path — runs synchronously.
    return JSONResponse(
        process_and_save_frames(
            segments=segments,
            was_capped=was_capped,
            user_id=user_id,
            channel_id=channel_id,
            base_url=base_url,
            settings=settings,
            flags=flags,
        )
    )


def _segments_preview(segments: list[tuple[str, str]], *, max_chars: int = 80) -> str:
    """Compact one-liner for the ack message — joins inputs with ``; ``
    and truncates to keep Slack ephemeral text from wrapping awkwardly."""
    joined = " ; ".join(p for _, p in segments)
    if len(joined) <= max_chars:
        return joined
    return joined[: max_chars - 1] + "…"


# ---------------------------------------------------------------------------
# Cloud Tasks worker endpoint
# ---------------------------------------------------------------------------
@app.post("/internal/process")
async def internal_process(request: Request) -> Response:
    """Worker for the trajectory path. Cloud Tasks dispatches POSTs here
    with an OIDC bearer token; we verify it, claim an idempotency key
    in Firestore, run OPSIN + RDKit for every segment, save the
    trajectory, and POST the result to Slack's ``response_url``.

    Response codes drive Cloud Tasks' retry decision:

      * 204 — done (success or user error already reported to Slack).
              CT will NOT retry.
      * 403 — OIDC verification failed. CT will NOT retry.
      * 500 — transient infrastructure failure (Firestore down, etc).
              CT will retry per the queue's backoff config.

    Payload schema v2 (current):

        {
          "schema_version": 2,
          "kind": "trajectory",
          "segments": [{"kind": "smiles"|"name", "payload": str}, ...],
          "was_capped": bool,
          "flags": {"public": bool, "label": bool, "no_3d": bool},
          "response_url": str,
          "user_id": str | None,
          "channel_id": str | None,
          "base_url": str,
          "idempotency_key": str,
        }
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

    if body.get("schema_version") != 2 or body.get("kind") != "trajectory":
        logger.warning(
            "internal_process_bad_schema",
            extra={"error_kind": "BadSchema"},
        )
        return PlainTextResponse("bad request", status_code=400)

    raw_segments = body.get("segments") or []
    segments: list[tuple[str, str]] = []
    for seg in raw_segments:
        kind = seg.get("kind")
        payload = seg.get("payload")
        if kind not in ("smiles", "name") or not isinstance(payload, str) or not payload:
            return PlainTextResponse("bad request", status_code=400)
        segments.append((kind, payload))

    was_capped = bool(body.get("was_capped"))
    response_url: str = body.get("response_url") or ""
    user_id: str | None = body.get("user_id")
    channel_id: str | None = body.get("channel_id")
    base_url: str = body.get("base_url") or ""
    idempotency_key: str = body.get("idempotency_key") or ""
    raw_flags = body.get("flags") or {}
    flags = {
        "public": bool(raw_flags.get("public")),
        "label": bool(raw_flags.get("label")),
        "no_3d": bool(raw_flags.get("no_3d")),
    }
    if not segments or not response_url or not base_url or not idempotency_key:
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

    payload_resp = process_and_save_frames(
        segments=segments,
        was_capped=was_capped,
        user_id=user_id,
        channel_id=channel_id,
        base_url=base_url,
        settings=settings,
        flags=flags,
    )
    slack_dispatch.post_to_response_url(response_url, payload_resp)
    return PlainTextResponse("", status_code=204)


# ---------------------------------------------------------------------------
# Viewer endpoints
# ---------------------------------------------------------------------------
def _enrich_frame_meta(frame: dict[str, Any]) -> dict[str, Any]:
    """Compute formula / MW per frame for the viewer overlay.

    The values are derived on read rather than stored in Firestore so
    we don't have to keep the document in lockstep with new descriptors
    (e.g. logP, TPSA) added later.
    """
    enriched = dict(frame)
    molblock = frame.get("molblock")
    if molblock:
        formula, mw = molblock_to_formula_and_weight(molblock)
        enriched["formula"] = formula
        enriched["mol_weight"] = mw
    else:
        enriched["formula"] = None
        enriched["mol_weight"] = None
    return enriched


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

    raw_frames = data.get("frames") or []
    frames = [_enrich_frame_meta(f) for f in raw_frames]

    # ``?mode=2d`` (from /mol --no-3d, or hand-typed) renders the static
    # SVG page instead of 3Dmol.js (#3). Each frame gets its own SVG.
    if request.query_params.get("mode") == "2d":
        for f in frames:
            f["svg_text"] = (
                molblock_to_svg(f["molblock"]) if f.get("molblock") else None
            )
        return HTMLResponse(
            templates.render_viewer_2d_html(
                frames=frames,
                mol_id=mol_id,
            )
        )

    return HTMLResponse(
        templates.render_viewer_html(
            frames=frames,
            mol_id=mol_id,
        )
    )


@app.get("/view/", response_class=HTMLResponse)
@app.get("/view", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def view_bare() -> HTMLResponse:
    return HTMLResponse(
        templates.render_viewer_html(frames=[], mol_id=None)
    )


# Silence the noisy 404 for /favicon.ico without serving anything.
@app.get("/favicon.ico")
def favicon() -> PlainTextResponse:
    return PlainTextResponse("", status_code=204)
