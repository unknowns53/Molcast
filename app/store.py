"""Firestore wrapper for the ``molecules`` collection (see §6.5).

Note on the lazy client: ``firestore.Client()`` discovers credentials at
construction time, which means importing this module in a test
environment without GCP credentials would otherwise blow up at import.
We instantiate on first use only.

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


def save_molecule(
    *,
    mol_id: str,
    smiles: str,
    molblock: str,
    input_name: str | None,
    created_by: str | None,
    channel_id: str | None,
    collection: str,
    retention_days: int,
) -> None:
    now = _dt.datetime.now(_dt.timezone.utc)
    expires_at = now + _dt.timedelta(days=retention_days)
    doc = {
        "id": mol_id,
        "smiles": smiles,
        "input_name": input_name,
        "molblock": molblock,
        "created_at": now,
        "expires_at": expires_at,
        "created_by": created_by,
        "channel_id": channel_id,
    }
    _client().collection(collection).document(mol_id).set(doc)


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
    return data
