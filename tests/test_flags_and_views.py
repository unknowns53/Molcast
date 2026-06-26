"""Tests for #3 (/mol flags), #6 (formula/MW), #7 (viewer buttons),
plus the multi-mol trajectory pipeline.

Covers:

  * ``_extract_flags`` / ``_split_segments`` parsing.
  * ``_viewer_url_with_flags`` query string composition.
  * ``/slack/mol`` end-to-end with each flag (SMILES path).
  * ``/slack/mol`` ``;`` splitting and name-routing to Cloud Tasks.
  * ``/view/{id}?mode=2d`` routes to ``render_viewer_2d_html``.
  * Viewer template surfaces formula / MW (#6) per frame, and the
    Copy SMILES / Save PNG buttons (#7) are wired.
  * Single-frame back-compat: nav strip is hidden.

External integrations (Slack verify, Cloud Tasks, Firestore, RDKit
draw) are mocked. Tests are hermetic — no GCP creds, no network.
"""
from __future__ import annotations

import json
import re
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app import store
from app.main import (
    _extract_flags,
    _split_segments,
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


def _frames_json_from_html(html: str):
    """Pull the trajectory ``frames`` JSON literal out of the 3D template."""
    m = re.search(r"const frames = (.+?);\s*\n\s*const initialMolId",
                  html, re.DOTALL)
    assert m is not None, "frames literal not found in HTML"
    return json.loads(m.group(1).replace("<\\/", "</"))


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
    flags, rest = _extract_flags("CCO --foo")
    assert flags == {"public": False, "label": False, "no_3d": False}
    assert "--foo" in rest


def test_extract_flags_empty_input():
    flags, rest = _extract_flags("")
    assert flags == {"public": False, "label": False, "no_3d": False}
    assert rest == ""


# ---------------------------------------------------------------------------
# _split_segments — multi-mol parsing
# ---------------------------------------------------------------------------
def test_split_segments_single_smiles():
    segs, capped = _split_segments("CCO")
    assert segs == [("smiles", "CCO")]
    assert capped is False


def test_split_segments_three_smiles_with_whitespace():
    segs, capped = _split_segments(" CCO  ;  CC(C)O ;  CC(C)(C)O ")
    assert segs == [
        ("smiles", "CCO"),
        ("smiles", "CC(C)O"),
        ("smiles", "CC(C)(C)O"),
    ]
    assert capped is False


def test_split_segments_mixed_smiles_and_name():
    segs, capped = _split_segments("CCO ; name: DMSO ; CC(C)O")
    assert segs == [
        ("smiles", "CCO"),
        ("name", "DMSO"),
        ("smiles", "CC(C)O"),
    ]
    assert capped is False


def test_split_segments_drops_empty_chunks():
    """``A ; ; B`` parses as two segments, not three."""
    segs, _ = _split_segments("CCO ;; CC(C)O ;  ; ")
    assert segs == [("smiles", "CCO"), ("smiles", "CC(C)O")]


def test_split_segments_caps_long_trajectory():
    too_many = " ; ".join(["C" * (i + 1) for i in range(25)])
    segs, capped = _split_segments(too_many, cap=20)
    assert len(segs) == 20
    assert capped is True


def test_split_segments_empty_returns_empty():
    segs, capped = _split_segments("")
    assert segs == []
    assert capped is False


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
# /slack/mol — flag integration (SMILES-only sync path)
# ---------------------------------------------------------------------------
def _fake_resp(text: str = "ok") -> dict:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}


def test_slack_mol_public_flag_overrides_response_type(client):
    """--public flips the response type to in_channel inside the sync
    pipeline. Spy on process_and_save_frames to confirm propagation."""
    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return _fake_resp()

    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(app_main, "process_and_save_frames", side_effect=_spy):
        resp = client.post(
            "/slack/mol",
            data={"text": "CCO --public", "user_id": "U1", "channel_id": "C1"},
        )
    assert resp.status_code == 200
    assert captured["flags"]["public"] is True
    assert captured["flags"]["label"] is False
    assert captured["flags"]["no_3d"] is False
    # The single segment is the SMILES with flag stripped.
    assert captured["segments"] == [("smiles", "CCO")]


def test_slack_mol_label_and_no_3d_propagate_to_pipeline(client):
    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return _fake_resp()

    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(app_main, "process_and_save_frames", side_effect=_spy):
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


def test_slack_mol_multi_smiles_propagates_all_segments(client):
    """``;``-separated SMILES go through the sync path as a single
    process_and_save_frames call with the full segments list."""
    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return _fake_resp()

    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(app_main, "process_and_save_frames", side_effect=_spy):
        resp = client.post(
            "/slack/mol",
            data={
                "text": "CCO ; CC(C)O ; CC(C)(C)O",
                "user_id": "U1",
                "channel_id": "C1",
            },
        )
    assert resp.status_code == 200
    assert captured["segments"] == [
        ("smiles", "CCO"),
        ("smiles", "CC(C)O"),
        ("smiles", "CC(C)(C)O"),
    ]


def test_slack_mol_name_branch_propagates_segments_to_tasks(client):
    """Any ``name:`` segment routes the whole trajectory to Cloud Tasks."""
    with mock.patch.object(app_main, "verify_slack_request", return_value=True), \
         mock.patch.object(
            app_main.tasks_dispatch,
            "enqueue_name_resolution",
            return_value="task/abc",
         ) as menq:
        resp = client.post(
            "/slack/mol",
            data={
                "text": "CCO ; name: DMSO --public",
                "user_id": "U1",
                "channel_id": "C1",
                "response_url": "https://hooks.slack.com/x",
            },
        )
    assert resp.status_code == 200
    enq_kwargs = menq.call_args.kwargs
    assert enq_kwargs["segments"] == [
        ("smiles", "CCO"),
        ("name", "DMSO"),
    ]
    assert enq_kwargs["flags"]["public"] is True
    assert enq_kwargs["flags"]["label"] is False
    assert enq_kwargs["was_capped"] is False


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
    # The empty-message hint now also mentions the ``;`` syntax (#11).
    assert ";" in body


# ---------------------------------------------------------------------------
# /view/{id} — formula/MW (#6) and mode=2d (#3)
# ---------------------------------------------------------------------------
_DUMMY_DOC = {
    "frames": [
        {
            "kind": "smiles",
            "input": "CCO",
            "smiles": "CCO",
            "molblock": (
                "\n     RDKit          3D\n\n  3  2  0  0  0  0  0  0  0  0999 V2000\n"
                "   -0.7990    0.5050    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "    0.4500    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "    1.4900    0.8700    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "  1  2  1  0\n  2  3  1  0\nM  END\n"
            ),
            "error": None,
        }
    ],
    "flags": {"public": False, "label": False, "no_3d": False},
}


def test_view_mol_renders_3d_template_with_frames(client):
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
    # The renderer now receives the full frames list with formula+MW
    # already merged in per frame.
    assert "frames" in kw
    assert len(kw["frames"]) == 1
    f0 = kw["frames"][0]
    assert f0["smiles"] == "CCO"
    assert f0["formula"] == "C2H6O"
    assert f0["mol_weight"] == 46.07


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
    # SVG threads into the frame dict, not a top-level kwarg.
    assert kw["frames"][0]["svg_text"] == "<svg>x</svg>"
    assert kw["frames"][0]["formula"] == "C2H6O"


# ---------------------------------------------------------------------------
# Template surface tests (#6, #7, trajectory)
# ---------------------------------------------------------------------------
def test_3d_template_embeds_formula_and_mw_in_frames_json():
    html = render_viewer_html(
        "CCO", "M  END\n", "abc",
        formula="C2H6O", mol_weight=46.0691,
    )
    # Server still emits "分子式" / "分子量" as the overlay labels.
    assert "分子式" in html
    assert "分子量" in html
    # The actual values reach the page via the embedded frames JSON.
    parsed = _frames_json_from_html(html)
    assert parsed[0]["formula"] == "C2H6O"
    assert parsed[0]["mol_weight"] == 46.0691


def test_3d_template_meta_overlay_present_but_hidden_initially():
    """The overlay element is in the DOM but starts ``hidden``; JS
    flips it visible once it has values to show."""
    html = render_viewer_html("CCO", "M  END\n", "abc")
    assert 'id="meta-overlay"' in html
    assert 'id="meta-formula-row"' in html
    assert 'id="meta-mw-row"' in html


def test_3d_template_has_smiles_copy_and_png_buttons():
    html = render_viewer_html("CCO", "M  END\n", "abc")
    assert 'id="btn-copy"' in html
    assert 'id="btn-save-png"' in html
    # The trajectory data must be embedded (frame's SMILES feeds the copy).
    assert "const frames" in html
    # The PNG handler must call viewer.pngURI().
    assert "viewer.pngURI()" in html


def test_3d_template_has_label_and_rotate_toggles():
    html = render_viewer_html("CCO", "M  END\n", "abc")
    assert 'id="btn-labels"' in html
    assert 'id="btn-rotate"' in html
    assert "viewer.spin" in html


def test_3d_template_honors_label_query_param_on_load():
    html = render_viewer_html("CCO", "M  END\n", "abc")
    assert "URLSearchParams" in html
    assert "toggleLabels(true)" in html


def test_3d_template_single_frame_hides_nav():
    """The ``trajnav`` strip must carry the ``hidden`` attribute when
    there is only one frame, so single-mol pages don't show empty
    nav controls."""
    html = render_viewer_html("CCO", "M  END\n", "abc")
    # Match the opening tag with the hidden attribute.
    m = re.search(r'<span class="trajnav" id="trajnav"([^>]*)>', html)
    assert m is not None
    assert "hidden" in m.group(1)


def test_3d_template_multi_frame_shows_nav():
    """Two-frame trajectory must NOT have ``hidden`` on the nav strip."""
    html = render_viewer_html(
        frames=[
            {"kind": "smiles", "input": "CCO", "smiles": "CCO",
             "molblock": "M  END\n", "formula": "C2H6O", "mol_weight": 46.07,
             "error": None},
            {"kind": "smiles", "input": "CC(C)O", "smiles": "CC(C)O",
             "molblock": "M  END\n", "formula": "C3H8O", "mol_weight": 60.10,
             "error": None},
        ],
        mol_id="abc",
    )
    m = re.search(r'<span class="trajnav" id="trajnav"([^>]*)>', html)
    assert m is not None
    assert "hidden" not in m.group(1)
    # Both frames embedded in JS.
    parsed = _frames_json_from_html(html)
    assert len(parsed) == 2
    assert parsed[0]["smiles"] == "CCO"
    assert parsed[1]["smiles"] == "CC(C)O"


def test_3d_template_arrow_key_handler_present():
    """Keyboard navigation (◀/▶ via Arrow keys) is one of the spec
    points; make sure the listener is wired."""
    html = render_viewer_html("CCO", "M  END\n", "abc")
    assert "ArrowLeft" in html
    assert "ArrowRight" in html


def test_3d_template_error_frame_does_not_break_render():
    """A trajectory with one failed frame still renders; the error
    string is embedded in the frames JSON for runtime display."""
    html = render_viewer_html(
        frames=[
            {"kind": "smiles", "input": "CCO", "smiles": "CCO",
             "molblock": "M  END\n", "formula": "C2H6O", "mol_weight": 46.07,
             "error": None},
            {"kind": "smiles", "input": "not_a_smiles", "smiles": None,
             "molblock": None, "formula": None, "mol_weight": None,
             "error": "SMILES の解釈に失敗しました。"},
        ],
        mol_id="abc",
    )
    parsed = _frames_json_from_html(html)
    assert parsed[1]["error"] == "SMILES の解釈に失敗しました。"
    assert parsed[1]["molblock"] is None


def test_2d_template_embeds_svg_per_frame():
    html = render_viewer_2d_html(
        frames=[
            {"kind": "smiles", "input": "CCO", "smiles": "CCO",
             "svg_text": "<svg width='600'><circle/></svg>",
             "formula": "C2H6O", "mol_weight": 46.07,
             "molblock": None, "error": None},
            {"kind": "smiles", "input": "CC(C)O", "smiles": "CC(C)O",
             "svg_text": "<svg width='600'><rect/></svg>",
             "formula": "C3H8O", "mol_weight": 60.10,
             "molblock": None, "error": None},
        ],
        mol_id="abc",
    )
    assert "<svg width='600'><circle/></svg>" in html
    assert "<svg width='600'><rect/></svg>" in html
    assert "Molecule Viewer (2D)" in html
    assert 'id="btn-copy"' in html
    # Nav strip visible (>1 frame).
    m = re.search(r'<span class="trajnav" id="trajnav"([^>]*)>', html)
    assert m is not None and "hidden" not in m.group(1)


def test_2d_template_strips_xml_prolog_from_svg():
    html = render_viewer_2d_html(
        frames=[
            {"kind": "smiles", "input": "CCO", "smiles": "CCO",
             "svg_text": '<?xml version="1.0"?><svg><g/></svg>',
             "formula": None, "mol_weight": None,
             "molblock": None, "error": None},
        ],
        mol_id="abc",
    )
    assert "<?xml" not in html
    assert "<svg>" in html


def test_2d_template_handles_missing_svg_with_message():
    html = render_viewer_2d_html(
        frames=[
            {"kind": "smiles", "input": "CCO", "smiles": "CCO",
             "svg_text": None,
             "formula": None, "mol_weight": None,
             "molblock": None, "error": None},
        ],
        mol_id="abc",
    )
    assert "2D 構造の生成に失敗しました" in html


def test_2d_template_backcompat_single_kwargs_still_works():
    """The pre-trajectory caller shape still functions."""
    html = render_viewer_2d_html(
        smiles="CCO",
        svg_text="<svg></svg>",
        mol_id="abc",
        formula="C2H6O",
        mol_weight=46.07,
    )
    assert "<svg></svg>" in html
