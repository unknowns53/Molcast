"""Tests for §6.5 (Firestore wrapper). Firestore itself is mocked; this
exercise only covers the wiring (collection name routing, expired-vs-
missing sentinel, retention-day arithmetic)."""
from __future__ import annotations

import datetime as _dt
from unittest import mock

from app import store


class _FakeSnapshot:
    def __init__(self, data: dict | None):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return self._data


class _FakeDocument:
    def __init__(self):
        self.set_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self._snapshot: _FakeSnapshot = _FakeSnapshot(None)
        self._exists_for_create: bool = False

    def set(self, data: dict) -> None:
        self.set_calls.append(data)

    def create(self, data: dict) -> None:
        from google.api_core import exceptions as gax

        if self._exists_for_create:
            raise gax.AlreadyExists("doc already exists")
        self._exists_for_create = True
        self.create_calls.append(data)

    def get(self) -> _FakeSnapshot:
        return self._snapshot

    def stage(self, data: dict | None) -> None:
        self._snapshot = _FakeSnapshot(data)


class _FakeCollection:
    def __init__(self):
        self.docs: dict[str, _FakeDocument] = {}

    def document(self, key: str) -> _FakeDocument:
        return self.docs.setdefault(key, _FakeDocument())


class _FakeClient:
    def __init__(self):
        self.collections: dict[str, _FakeCollection] = {}

    def collection(self, name: str) -> _FakeCollection:
        return self.collections.setdefault(name, _FakeCollection())


def test_new_mol_id_is_url_safe_and_unguessable():
    a = store.new_mol_id()
    b = store.new_mol_id()
    assert a != b
    # token_urlsafe(16) -> 22 chars.
    assert len(a) == 22


def test_save_molecule_writes_expected_fields():
    fake = _FakeClient()
    with mock.patch.object(store, "_client", return_value=fake):
        store.save_molecule(
            mol_id="abc",
            smiles="CCO",
            molblock="MOLDATA",
            input_name=None,
            created_by="U1",
            channel_id="C1",
            collection="molecules",
            retention_days=7,
        )
    doc = fake.collections["molecules"].docs["abc"]
    assert len(doc.set_calls) == 1
    record = doc.set_calls[0]
    assert record["id"] == "abc"
    assert record["smiles"] == "CCO"
    assert record["molblock"] == "MOLDATA"
    assert record["input_name"] is None
    assert record["created_by"] == "U1"
    assert record["channel_id"] == "C1"
    # expires_at - created_at = retention_days (with sub-second tolerance).
    delta = record["expires_at"] - record["created_at"]
    assert abs(delta - _dt.timedelta(days=7)) < _dt.timedelta(seconds=1)


def test_get_molecule_missing_returns_none():
    fake = _FakeClient()
    with mock.patch.object(store, "_client", return_value=fake):
        assert store.get_molecule("nope", collection="molecules") is None


def test_get_molecule_live_returns_data():
    fake = _FakeClient()
    now = _dt.datetime.now(_dt.timezone.utc)
    fake.collection("molecules").document("abc").stage({
        "id": "abc",
        "smiles": "CCO",
        "molblock": "M",
        "expires_at": now + _dt.timedelta(days=7),
    })
    with mock.patch.object(store, "_client", return_value=fake):
        out = store.get_molecule("abc", collection="molecules")
    assert out is not None
    assert out["smiles"] == "CCO"


def test_get_molecule_expired_returns_sentinel():
    fake = _FakeClient()
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
    fake.collection("molecules").document("abc").stage({
        "id": "abc",
        "smiles": "CCO",
        "molblock": "M",
        "expires_at": past,
    })
    with mock.patch.object(store, "_client", return_value=fake):
        out = store.get_molecule("abc", collection="molecules")
    assert out == {"expired": True}


def test_naive_expires_at_treated_as_utc():
    fake = _FakeClient()
    past_naive = (
        _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        - _dt.timedelta(seconds=1)
    )
    fake.collection("molecules").document("abc").stage({
        "expires_at": past_naive,
    })
    with mock.patch.object(store, "_client", return_value=fake):
        out = store.get_molecule("abc", collection="molecules")
    assert out == {"expired": True}


def test_claim_idempotency_key_first_call_returns_true():
    fake = _FakeClient()
    with mock.patch.object(store, "_client", return_value=fake):
        ok = store.claim_idempotency_key(
            "abc123", collection="molecules_idempotency", ttl_seconds=3600
        )
    assert ok is True
    doc = fake.collections["molecules_idempotency"].docs["abc123"]
    assert len(doc.create_calls) == 1
    record = doc.create_calls[0]
    assert record["key"] == "abc123"
    delta = record["expires_at"] - record["claimed_at"]
    assert abs(delta - _dt.timedelta(seconds=3600)) < _dt.timedelta(seconds=1)


def test_claim_idempotency_key_second_call_returns_false():
    fake = _FakeClient()
    with mock.patch.object(store, "_client", return_value=fake):
        first = store.claim_idempotency_key(
            "abc123", collection="molecules_idempotency", ttl_seconds=3600
        )
        second = store.claim_idempotency_key(
            "abc123", collection="molecules_idempotency", ttl_seconds=3600
        )
    assert first is True
    assert second is False
    # Only the first claim persisted.
    doc = fake.collections["molecules_idempotency"].docs["abc123"]
    assert len(doc.create_calls) == 1
