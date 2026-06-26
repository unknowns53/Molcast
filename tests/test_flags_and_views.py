"""Tests for #3 (/mol flags), #6 (formula/MW), and #7 (viewer buttons).

Covers:

  * ``_extract_flags`` parsing — order independence, name: preservation.
  * ``_viewer_url_with_flags`` — query string composition.
  * ``/slack/mol`` end-to-end with each flag.
  * ``/view/{id}?mode=2d`` routes to ``render_viewer_2d_html``.
  * Viewer template surfaces formula / molecular weight (#6) and the
    SMILES-copy / PNG-save buttons (#7).

External integrations (Slack verify, Cloud Tasks, Firestore, RDKit
draw) are mocked. Tests are hermetic — no GCP creds, no network.
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app import store
from app.main import (
    _extract_flags,
    _viewer_url_with_flags,
    app,
)
from app.templates import render_viewer_2d_html, render_viewer_html


_BASE = "https://x.run.app"


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    monkeypatch.setenv("BASE_URL", _BASE)
    monkeypatch.setenv("TASKS_PROJECT_ID", "example-proj")
    monkeypatch.setenv("TASKS_QUEUE_ID", "molcast-name-resolution")
    monkeypatch.setenv("TASKS_LOCATION", "asia-northeast1")
    monkeypatch.setenv("TASKS_INVOKER_SA", "x@y.iam.gserviceaccount.com")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "dummy")
    from app.config import reload_settings

    reload_settings()
    yield
    reload_settings()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# _extract_flags
# ---------------------------------------------------------------------------
def test_extract_flags_no_flags_returns_text_unchanged():
    flags, rest = _extract_flags("CCO")
    assert flags == {"public": False, "label": False, "no_3d": False}
    assert rest == "CCO"


def test_extract_flags_all_three_in_any_order():
    flags, rest = _extract_flags("--label CCO --public --no-3d")
    assert flags == {"public": True, "label": True, "no_3d": True}
    assert rest == "CCO"


def test_extract_flags_with_name_branch():
    flags, rest = _extract_flags("name: ethanol --public")
    assert flags["public"] is True
    assert rest == "name: ethanol"


def test_extract_flags_unknown_flag_left_in_payload():
    """An unknown --foo must NOT be silently dropped — it falls through
    to SMILES classification and OPSIN/RDKit will surface the typo."""
    flags, rest = _extract_flags("CCO --foo")
    assert flags == {"public": False, "label": False, "no_3d": False}
    assert "--foo" in rest


def test_extract_flags_empty_input():
    flags, rest = _extract_flags("")
    assert flags == {"public": False, "label": False, "no_3d": False}
    assert rest == ""


# ---------------------------------------------------------------------------
# _viewer_url_with_flags
# ---------------------------------------------------------------------------
def test_viewer_url_no_flags_has_no_query():
    url = _viewer_url_with_flags(_BASE, "abc", {})
    assert url == f"{_BASE}/view/abc"


def test_viewer_url_label_only_adds_label_query():
    url = _viewer_url_with_flags(_BASE, "abc", {"label": True})
    assert url == f"{_BASE}/view/abc?label=1"


def test_viewer_url_label_and_no_3d_combine_with_ampersand():
    url = _viewer_url_with_flags(_BASE, "abc", {"label": True, "no_3d": True})
    assert "?" in url
    assert "label=1" in url
    assert "mode=2d" in url


def test_viewer_url_public_alone_does_not_appear_in_url():
    """--public is a response-type toggle, not a viewer-page concern."""
    url = _viewer_url_with_flags(_BASE, "abc", {"public": True})
    assert url == f"{_BASE}/view/abc"


# ---------------------------------------------------------------------------
# /slack/mol — flag integration (SMILES branch)
# ---------------------------------------------------------------------------
def _fake_sync(text: str = "ok") -> dict:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}


def test_slack_mol_public_flag_overrides_response_type(client):
    """--public must override the configured ephemeral default so the
    final SMILES-route message lands in_channel."""
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return _fake_sync()

    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(app_main, "_process_smiles_sync", side_effect=_spy):
        resp = client.post(
            "/slack/mol",
            data={"text": "CCO --public", "user_id": "U1", "channel_id": "C1"},
        )
    assert resp.status_code == 200
    assert captured["flags"]["public"] is True
    assert captured["flags"]["label"] is False
    assert captured["flags"]["no_3d"] is False
    # Residual text is the SMILES only — flags stripped before classify.
    assert captured["smiles"] == "CCO"


def test_slack_mol_label_and_no_3d_propagate_to_sync(client):
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return _fake_sync()

    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(app_main, "_process_smiles_sync", side_effect=_spy):
        resp = client.post(
            "/slack/mol",
            data={
                "text": "CCO --label --no-3d",
                "user_id": "U1",
                "channel_id": "C1",
            },
        )
    assert resp.status_code == 200
    assert captured["flags"] == {"public": False, "label": True, "no_3d": True}


def test_slack_mol_name_branch_propagates_flags_to_tasks_dispatch(client):
    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(
            app_main.tasks_dispatch,
            "enqueue_name_resolution",
            return_value="task/abc",
         ) as menq:
        resp = client.post(
            "/slack/mol",
            data={
                "text": "name: ethanol --public --label",
                "user_id": "U1",
                "channel_id": "C1",
                "response_url": "https://hooks.slack.com/x",
            },
        )
    assert resp.status_code == 200
    enq_kwargs = menq.call_args.kwargs
    assert enq_kwargs["name"] == "ethanol"
    assert enq_kwargs["flags"]["public"] is True
    assert enq_kwargs["flags"]["label"] is True
    assert enq_kwargs["flags"]["no_3d"] is False


def test_slack_mol_empty_hints_at_options(client):
    with mock.patch.object(app_main, "verify_slack_request", return_value=True):
        resp = client.post(
            "/slack/mol",
            data={"text": "", "user_id": "U1", "channel_id": "C1"},
        )
    assert resp.status_code == 200
    body = resp.json()["text"]
    assert "--public" in body
    assert "--label" in body
    assert "--no-3d" in body


# ---------------------------------------------------------------------------
# /view/{id} — formula/MW (#6) and mode=2d (#3)
# ---------------------------------------------------------------------------
_DUMMY_DOC = {
    "smiles": "CCO",
    # Minimal but legal MolBlock so RDKit can parse it. Ethanol with H.
    # We don't actually rely on the contents in these tests because the
    # rdkit helpers are mocked, but get_molecule returns this verbatim.
    "molblock": (
        "\n     RDKit          3D\n\n  3  2  0  0  0  0  0  0  0  0999 V2000\n"
        "   -0.7990    0.5050    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
        "    0.4500    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
        "    1.4900    0.8700    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\n"
        "  1  2  1  0\n  2  3  1  0\nM  END\n"
    ),
}


def test_view_mol_passes_formula_and_mw_to_3d_template(client):
    with mock.patch.object(store, "get_molecule", return_value=dict(_DUMMY_DOC)), \
         mock.patch.object(
            app_main,
            "molblock_to_formula_and_weight",
            return_value=("C2H6O", 46.07),
         ) as mfw, \
         mock.patch.object(app_main.templates, "render_viewer_html") as mrender:
        mrender.return_value = "<html>3D</html>"
        resp = client.get("/view/abc")
    assert resp.status_code == 200
    assert mfw.call_count == 1
    kw = mrender.call_args.kwargs
    assert kw["formula"] == "C2H6O"
    assert kw["mol_weight"] == 46.07


def test_view_mol_mode_2d_routes_to_svg_template(client):
    with mock.patch.object(store, "get_molecule", return_value=dict(_DUMMY_DOC)), \
         mock.patch.object(
            app_main,
            "molblock_to_formula_and_weight",
            return_value=("C2H6O", 46.07),
         ), \
         mock.patch.object(
            app_main,
            "molblock_to_svg",
            return_value="<svg>x</svg>",
         ) as msvg, \
         mock.patch.object(
            app_main.templates, "render_viewer_2d_html"
         ) as mrender2d, \
         mock.patch.object(
            app_main.templates, "render_viewer_html"
         ) as mrender3d:
        mrender2d.return_value = "<html>2D</html>"
        resp = client.get("/view/abc?mode=2d")
    assert resp.status_code == 200
    assert msvg.call_count == 1
    assert mrender2d.call_count == 1
    assert mrender3d.call_count == 0
    kw = mrender2d.call_args.kwargs
    assert kw["svg_text"] == "<svg>x</svg>"
    assert kw["formula"] == "C2H6O"
    assert kw["mol_weight"] == 46.07


# ---------------------------------------------------------------------------
# Template surface tests (#6, #7)
# ---------------------------------------------------------------------------
def test_3d_template_renders_meta_block_when_formula_and_mw_set():
    html = render_viewer_html(
        "CCO", "M  END\n", "abc",
        formula="C2H6O", mol_weight=46.0691,
    )
    assert "分子式" in html
    assert "C2H6O" in html
    assert "分子量" in html
    assert "46.07" in html


def test_3d_template_skips_meta_block_when_neither_set():
    html = render_viewer_html("CCO", "M  END\n", "abc")
    assert "分子式" not in html
    assert "分子量" not in html


def test_3d_template_has_smiles_copy_and_png_buttons():
    html = render_viewer_html("CCO", "M  END\n", "abc")
    assert 'id="btn-copy"' in html
    assert 'id="btn-save-png"' in html
    # The Copy handler reads initialSmiles, which must be embedded.
    assert "const initialSmiles" in html
    # The PNG handler must call viewer.pngURI().
    assert "viewer.pngURI()" in html


def test_3d_template_has_label_and_rotate_toggles():
    html = render_viewer_html("CCO", "M  END\n", "abc")
    assert 'id="btn-labels"' in html
    assert 'id="btn-rotate"' in html
    # Auto-rotate uses viewer.spin (3Dmol.js API).
    assert "viewer.spin" in html


def test_3d_template_honors_label_query_param_on_load():
    html = render_viewer_html("CCO", "M  END\n", "abc")
    # The IIFE must read URLSearchParams and call toggleLabels(true)
    # when ?label=1 is set (#3 --label propagation).
    assert "URLSearchParams" in html
    assert "label" in html
    assert "toggleLabels(true)" in html


def test_2d_template_embeds_svg_and_meta():
    html = render_viewer_2d_html(
        smiles="CCO",
        svg_text="<svg width='600'><circle/></svg>",
        mol_id="abc",
        formula="C2H6O",
        mol_weight=46.07,
    )
    assert "<svg" in html
    assert "C2H6O" in html
    assert "46.07" in html
    assert "Molecule Viewer (2D)" in html
    # Still has the Copy SMILES button.
    assert 'id="btn-copy"' in html


def test_2d_template_strips_xml_prolog_from_svg():
    html = render_viewer_2d_html(
        smiles="CCO",
        svg_text='<?xml version="1.0"?><svg><g/></svg>',
        mol_id="abc",
    )
    assert "<?xml" not in html
    assert "<svg>" in html


def test_2d_template_handles_missing_svg_gracefully():
    html = render_viewer_2d_html(
        smiles="CCO", svg_text=None, mol_id="abc"
    )
    assert "2D 構造の生成に失敗しました" in html
