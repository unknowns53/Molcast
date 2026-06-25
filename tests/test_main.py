"""End-to-end tests for the two-stage flow.

Covers:

  * ``POST /slack/mol`` for the ``name:`` branch — Cloud Tasks enqueue
    + ack-immediate.
  * ``POST /internal/process`` — OIDC verify, idempotency, OPSIN +
    RDKit + Firestore, ``response_url`` POST.

All external integrations (Slack signature verification, Cloud Tasks
client, Firestore, ``iupac_to_smiles``, ``post_to_response_url``) are
mocked. No GCP credentials or network required.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app import oidc_verify, store
from app.main import app
from app.rdkit_utils import MoleculeGenerationError
from app.tasks_dispatch import TasksConfigError


_BASE_URL = "https://mol-slack-viewer-xxxxxxxx-an.a.run.app"
_RESPONSE_URL = "https://hooks.slack.com/commands/T000/123/abc"
_PRINCIPAL = "molcast-ct-invoker@example-proj.iam.gserviceaccount.com"


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """Each test gets a fresh ``Settings`` so env overrides do not leak."""
    monkeypatch.setenv("BASE_URL", _BASE_URL)
    monkeypatch.setenv("TASKS_PROJECT_ID", "example-proj")
    monkeypatch.setenv("TASKS_QUEUE_ID", "molcast-name-resolution")
    monkeypatch.setenv("TASKS_LOCATION", "asia-northeast1")
    monkeypatch.setenv("TASKS_INVOKER_SA", _PRINCIPAL)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "dummy")
    from app.config import reload_settings

    reload_settings()
    yield
    reload_settings()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# /slack/mol — name: branch
# ---------------------------------------------------------------------------
def _name_form(name: str = "hexafluorobenzene", response_url: str = _RESPONSE_URL) -> dict:
    return {
        "text": f"name: {name}",
        "user_id": "U123",
        "channel_id": "C456",
        "response_url": response_url,
    }


def test_slack_mol_name_enqueues_task_and_acks_immediately(client):
    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(
            app_main.tasks_dispatch,
            "enqueue_name_resolution",
            return_value="projects/p/locations/l/queues/q/tasks/abc",
         ) as menq:
        resp = client.post("/slack/mol", data=_name_form())
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["response_type"] == "ephemeral"
    assert "処理中" in payload["text"]
    assert menq.call_count == 1
    enq_kwargs = menq.call_args.kwargs
    assert enq_kwargs["name"] == "hexafluorobenzene"
    assert enq_kwargs["response_url"] == _RESPONSE_URL
    assert enq_kwargs["user_id"] == "U123"
    assert enq_kwargs["channel_id"] == "C456"
    assert enq_kwargs["base_url"] == _BASE_URL


def test_slack_mol_name_without_response_url_returns_friendly_error(client):
    """Slack always sends ``response_url`` in practice, but defend
    against malformed input rather than KeyError-ing.
    """
    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(app_main.tasks_dispatch, "enqueue_name_resolution") as menq:
        resp = client.post(
            "/slack/mol",
            data=_name_form(response_url=""),
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert "response_url" in payload["text"]
    assert menq.call_count == 0


def test_slack_mol_name_tasks_config_error_returns_friendly_error(client):
    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(
            app_main.tasks_dispatch,
            "enqueue_name_resolution",
            side_effect=TasksConfigError("missing"),
         ):
        resp = client.post("/slack/mol", data=_name_form())
    assert resp.status_code == 200
    payload = resp.json()
    assert "管理者" in payload["text"]


# ---------------------------------------------------------------------------
# /slack/mol — SMILES branch still sync (regression guard)
# ---------------------------------------------------------------------------
def test_slack_mol_smiles_path_still_runs_sync(client):
    """SMILES route must not regress to two-stage; it still uses the
    sync pipeline that Phase 1 verified fits in the 3 s window.
    """
    fake_payload = {
        "response_type": "ephemeral",
        "replace_original": False,
        "text": "3D viewer generated: https://x/view/abc",
    }
    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(
            app_main, "_process_smiles_sync", return_value=fake_payload
         ) as mproc, \
         mock.patch.object(
            app_main.tasks_dispatch, "enqueue_name_resolution"
         ) as menq:
        resp = client.post(
            "/slack/mol",
            data={"text": "CCO", "user_id": "U1", "channel_id": "C1"},
        )
    assert resp.status_code == 200
    assert resp.json() == fake_payload
    assert mproc.call_count == 1
    assert menq.call_count == 0


# ---------------------------------------------------------------------------
# /internal/process — OIDC + schema guards
# ---------------------------------------------------------------------------
def _process_body(name: str = "hexafluorobenzene") -> dict:
    import hashlib

    idemp = hashlib.sha256(_RESPONSE_URL.encode()).hexdigest()[:32]
    return {
        "schema_version": 1,
        "kind": "name",
        "payload": name,
        "response_url": _RESPONSE_URL,
        "user_id": "U123",
        "channel_id": "C456",
        "base_url": _BASE_URL,
        "idempotency_key": idemp,
    }


def test_internal_process_bad_oidc_returns_403(client):
    with mock.patch.object(
        app_main, "verify_oidc_token", side_effect=oidc_verify.OIDCVerificationError()
    ):
        resp = client.post(
            "/internal/process",
            content=json.dumps(_process_body()),
            headers={"Authorization": "Bearer bad", "Content-Type": "application/json"},
        )
    assert resp.status_code == 403


def test_internal_process_bad_json_returns_400(client):
    with mock.patch.object(app_main, "verify_oidc_token", return_value={"email": _PRINCIPAL}):
        resp = client.post(
            "/internal/process",
            content="not json",
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
        )
    assert resp.status_code == 400


def test_internal_process_bad_schema_returns_400(client):
    with mock.patch.object(app_main, "verify_oidc_token", return_value={"email": _PRINCIPAL}):
        body = _process_body()
        body["schema_version"] = 99
        resp = client.post(
            "/internal/process",
            content=json.dumps(body),
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
        )
    assert resp.status_code == 400


def test_internal_process_missing_fields_returns_400(client):
    with mock.patch.object(app_main, "verify_oidc_token", return_value={"email": _PRINCIPAL}):
        body = _process_body()
        body["base_url"] = ""
        resp = client.post(
            "/internal/process",
            content=json.dumps(body),
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /internal/process — idempotency
# ---------------------------------------------------------------------------
def test_internal_process_duplicate_dispatch_skips_work(client):
    """Second dispatch with the same idempotency_key must short-circuit
    to 204 without calling OPSIN or posting to ``response_url``.
    """
    with mock.patch.object(app_main, "verify_oidc_token", return_value={"email": _PRINCIPAL}), \
         mock.patch.object(store, "claim_idempotency_key", return_value=False), \
         mock.patch.object(app_main, "iupac_to_smiles") as mopsin, \
         mock.patch.object(app_main.slack_dispatch, "post_to_response_url") as mpost:
        resp = client.post(
            "/internal/process",
            content=json.dumps(_process_body()),
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
        )
    assert resp.status_code == 204
    assert mopsin.call_count == 0
    assert mpost.call_count == 0


def test_internal_process_idempotency_infra_failure_returns_500(client):
    """If the idempotency claim itself blows up, CT must retry (5xx)."""
    with mock.patch.object(app_main, "verify_oidc_token", return_value={"email": _PRINCIPAL}), \
         mock.patch.object(
            store, "claim_idempotency_key", side_effect=RuntimeError("firestore down")
         ), \
         mock.patch.object(app_main.slack_dispatch, "post_to_response_url") as mpost:
        resp = client.post(
            "/internal/process",
            content=json.dumps(_process_body()),
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
        )
    assert resp.status_code == 500
    assert mpost.call_count == 0


# ---------------------------------------------------------------------------
# /internal/process — happy path
# ---------------------------------------------------------------------------
def test_internal_process_success_posts_viewer_url_to_response_url(client):
    fake_payload = {
        "response_type": "ephemeral",
        "replace_original": False,
        "text": "3D viewer generated: https://x/view/abc",
    }
    with mock.patch.object(app_main, "verify_oidc_token", return_value={"email": _PRINCIPAL}), \
         mock.patch.object(store, "claim_idempotency_key", return_value=True), \
         mock.patch.object(
            app_main, "iupac_to_smiles", return_value="Fc1c(F)c(F)c(F)c(F)c1F"
         ) as mopsin, \
         mock.patch.object(
            app_main, "_process_smiles_sync", return_value=fake_payload
         ) as mproc, \
         mock.patch.object(
            app_main.slack_dispatch, "post_to_response_url", return_value=True
         ) as mpost:
        resp = client.post(
            "/internal/process",
            content=json.dumps(_process_body()),
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
        )
    assert resp.status_code == 204
    assert mopsin.call_count == 1
    assert mproc.call_count == 1
    assert mproc.call_args.kwargs["smiles"] == "Fc1c(F)c(F)c(F)c(F)c1F"
    assert mproc.call_args.kwargs["input_name"] == "hexafluorobenzene"
    assert mpost.call_count == 1
    posted_url, posted_payload = mpost.call_args.args
    assert posted_url == _RESPONSE_URL
    assert posted_payload == fake_payload


def test_internal_process_opsin_user_error_posts_error_to_slack_204(client):
    """An OPSIN failure (user error) MUST post the error message to
    Slack and return 204 — retry would yield the same failure.
    """
    with mock.patch.object(app_main, "verify_oidc_token", return_value={"email": _PRINCIPAL}), \
         mock.patch.object(store, "claim_idempotency_key", return_value=True), \
         mock.patch.object(
            app_main,
            "iupac_to_smiles",
            side_effect=MoleculeGenerationError("OPSIN は体系名のみ対応です..."),
         ), \
         mock.patch.object(app_main, "_process_smiles_sync") as mproc, \
         mock.patch.object(
            app_main.slack_dispatch, "post_to_response_url", return_value=True
         ) as mpost:
        resp = client.post(
            "/internal/process",
            content=json.dumps(_process_body()),
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
        )
    assert resp.status_code == 204
    assert mproc.call_count == 0
    assert mpost.call_count == 1
    _, posted_payload = mpost.call_args.args
    assert "OPSIN" in posted_payload["text"]


# ---------------------------------------------------------------------------
# Local-dev flags (DEV_SKIP_SIGNATURE_VERIFICATION, DEV_INLINE_NAME_RESOLUTION)
# ---------------------------------------------------------------------------
def test_slack_mol_skips_signature_when_dev_flag_set(client, monkeypatch):
    """With DEV_SKIP_SIGNATURE_VERIFICATION=true, /slack/mol must accept
    requests that carry no signature header (or a wrong one). This is
    the local-dev path — tunnel + curl + Slack-without-prod-secret.
    """
    monkeypatch.setenv("DEV_SKIP_SIGNATURE_VERIFICATION", "true")
    from app.config import reload_settings

    reload_settings()
    fake_payload = {
        "response_type": "ephemeral",
        "replace_original": False,
        "text": "3D viewer generated: https://x/view/abc",
    }
    with mock.patch.object(app_main, "verify_slack_request") as mverify, \
         mock.patch.object(
            app_main, "_process_smiles_sync", return_value=fake_payload
         ):
        # No signature headers; in prod this would be 403.
        resp = client.post(
            "/slack/mol",
            data={"text": "CCO", "user_id": "U1", "channel_id": "C1"},
        )
    assert resp.status_code == 200
    assert resp.json() == fake_payload
    # verify_slack_request must NOT have been called at all when the
    # flag is on — short-circuit before HMAC work.
    assert mverify.call_count == 0


def test_slack_mol_enforces_signature_when_dev_flag_unset(client):
    """Regression: with the flag at its False default, /slack/mol still
    enforces signature verification (this is the prod path).
    """
    # The autouse fixture does not set DEV_SKIP_SIGNATURE_VERIFICATION,
    # so it defaults to False. Send a request with no/wrong signature
    # and patch verify_slack_request to return False.
    with mock.patch.object(app_main, "verify_slack_request", return_value=False):
        resp = client.post(
            "/slack/mol",
            data={"text": "CCO", "user_id": "U1", "channel_id": "C1"},
        )
    assert resp.status_code == 403


def test_slack_mol_name_uses_dev_dispatch_when_inline_flag_set(client, monkeypatch):
    """With DEV_INLINE_NAME_RESOLUTION=true, the name: branch must route
    through dev_dispatch.run_inline_name_resolution, NOT
    tasks_dispatch.enqueue_name_resolution. Cloud Tasks is unavailable
    in dev so the prod path would fail with TasksConfigError.
    """
    monkeypatch.setenv("DEV_INLINE_NAME_RESOLUTION", "true")
    from app.config import reload_settings

    reload_settings()
    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(
            app_main.dev_dispatch,
            "run_inline_name_resolution",
            return_value="dev-inline-task/abc",
         ) as minline, \
         mock.patch.object(
            app_main.tasks_dispatch, "enqueue_name_resolution"
         ) as menq:
        resp = client.post("/slack/mol", data=_name_form())
    assert resp.status_code == 200
    payload = resp.json()
    assert "処理中" in payload["text"]
    assert minline.call_count == 1
    assert menq.call_count == 0
    kwargs = minline.call_args.kwargs
    assert kwargs["name"] == "hexafluorobenzene"
    assert kwargs["response_url"] == _RESPONSE_URL
    assert kwargs["base_url"] == _BASE_URL


def test_slack_mol_name_uses_tasks_dispatch_when_inline_flag_unset(client):
    """Regression: with the flag False (autouse default), the name: branch
    goes through Cloud Tasks, not the dev inline path.
    """
    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(
            app_main.tasks_dispatch,
            "enqueue_name_resolution",
            return_value="projects/p/locations/l/queues/q/tasks/abc",
         ) as menq, \
         mock.patch.object(
            app_main.dev_dispatch, "run_inline_name_resolution"
         ) as minline:
        resp = client.post("/slack/mol", data=_name_form())
    assert resp.status_code == 200
    assert menq.call_count == 1
    assert minline.call_count == 0
