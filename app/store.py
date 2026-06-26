"""Firestore wrapper for the ``molecules`` collection (see §6.5).

Note on the lazy client: ``firestore.Client()`` discovers credentials at
construction time, which means importing this module in a test
environment without GCP credentials would otherwise blow up at import.
We instantiate on first use only.

Document shape (current):

    {
        "id":          str,
        "frames":      list[ Frame ],          # always present, >= 1 element
        "flags":       {"public": bool, "label": bool, "no_3d": bool},
        "created_at":  datetime,
        "expires_at":  datetime,
        "created_by":  str | None,
        "channel_id":  str | None,
    }

Frame = {
    "kind":      "smiles" | "name",
    "input":     str,            # original ``/mol`` token
    "smiles":    str | None,     # resolved SMILES (None on error before RDKit)
    "molblock":  str | None,     # 3D MolBlock (None on error)
    "error":     str | None,     # user-facing message when this frame failed
}

Older single-molecule documents (pre-trajectory) had top-level
``smiles`` / ``molblock`` / ``input_name`` instead of a ``frames`` list.
:func:`get_molecule` normalises both shapes to the new form so callers
only ever see ``data["frames"]``.

Note on the expired-vs-missing distinction (§6.5 last paragraph):
``get_molecule`` returns ``None`` for "no such id" and the sentinel
``{"expired": True}`` for "id exists but TTL elapsed" so the viewer can
distinguish 404 from the 'expired' page.
"""
from __future__ import annotations

import datetime as _dt
import secrets
from typing import Any


def new_mol_id() -> str:
    """Random URL-safe id used as the Firestore document key.

    16 bytes of entropy -> 22 url-safe chars; this matches the §4.2
    requirement that the id be unguessable so a leaked URL is the only
    practical way for a third party to reach a viewer.
    """
    return secrets.token_urlsafe(16)


def _client():
    # Imported lazily so tests can patch the module before Firestore
    # tries to discover credentials (see test_store.py).
    from google.cloud import firestore

    return firestore.Client()


def save_trajectory(
    *,
    mol_id: str,
    frames: list[dict[str, Any]],
    flags: dict[str, bool] | None,
    created_by: str | None,
    channel_id: str | None,
    collection: str,
    retention_days: int,
) -> None:
    """Persist a trajectory (1..N frames) under one document key.

    The single-molecule case is just ``frames=[only_frame]`` — the
    template renders the navigation strip only when ``len(frames) > 1``.
    Each frame must carry ``kind`` / ``input``; ``smiles`` / ``molblock``
    are optional (a frame whose RDKit step failed has both ``None`` and
    an ``error`` string).
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    expires_at = now + _dt.timedelta(days=retention_days)
    doc = {
        "id": mol_id,
        "frames": [_clean_frame(f) for f in frames],
        "flags": flags or {"public": False, "label": False, "no_3d": False},
        "created_at": now,
        "expires_at": expires_at,
        "created_by": created_by,
        "channel_id": channel_id,
    }
    _client().collection(collection).document(mol_id).set(doc)


def _clean_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """Normalise a frame dict before persistence.

    Guarantees every frame has the same keys (Firestore is schemaless
    but consumer code is easier when keys are stable). Unknown keys
    are dropped so a forgotten kwarg from the caller does not pollute
    the document.
    """
    return {
        "kind": frame.get("kind") or "smiles",
        "input": frame.get("input") or "",
        "smiles": frame.get("smiles"),
        "molblock": frame.get("molblock"),
        "error": frame.get("error"),
    }


def get_molecule(mol_id: str, *, collection: str) -> dict[str, Any] | None:
    snapshot = _client().collection(collection).document(mol_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    expires_at = data.get("expires_at")
    if isinstance(expires_at, _dt.datetime):
        # Firestore returns timezone-aware UTC datetimes; normalise just
        # in case a test stub returns naive ones.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=_dt.timezone.utc)
        if expires_at < _dt.datetime.now(_dt.timezone.utc):
            return {"expired": True}

    # Normalise legacy single-molecule documents to the frames shape so
    # the viewer only has to handle one schema.
    if "frames" not in data:
        legacy_frame = {
            "kind": "name" if data.get("input_name") else "smiles",
            "input": data.get("input_name") or data.get("smiles") or "",
            "smiles": data.get("smiles"),
            "molblock": data.get("molblock"),
            "error": None,
        }
        data["frames"] = [legacy_frame]
    data.setdefault(
        "flags", {"public": False, "label": False, "no_3d": False}
    )
    return data


def claim_idempotency_key(
    key: str, *, collection: str, ttl_seconds: int
) -> bool:
    """Atomically claim ``key`` in ``collection``.

    Used by ``/internal/process`` to dedupe Cloud Tasks at-least-once
    re-deliveries (worker crash → re-dispatch). Returns ``True`` on
    first claim, ``False`` on re-attempt (the doc already exists).
    Firestore's ``document.create()`` raises ``AlreadyExists`` on the
    second call which we translate to ``False`` — the caller treats
    that as "another worker already handled this task" and short-
    circuits to 204.

    The ``expires_at`` field is set so a Firestore TTL policy can
    sweep stale claims; without that policy, claims accumulate
    forever. Setting it without the policy is harmless — it's just
    persisted metadata until an operator turns the TTL on.
    """
    # Imported here to keep top-level import light for unit tests that
    # don't touch this code path.
    from google.api_core import exceptions as gax

    now = _dt.datetime.now(_dt.timezone.utc)
    expires_at = now + _dt.timedelta(seconds=ttl_seconds)
    doc = {
        "key": key,
        "claimed_at": now,
        "expires_at": expires_at,
    }
    try:
        _client().collection(collection).document(key).create(doc)
    except gax.AlreadyExists:
        return False
    return True
