"""Tests for §7 (viewer HTML rendering and JS embedding safety)."""
from __future__ import annotations

import json
import re

from app.templates import (
    render_expired_html,
    render_not_found_html,
    render_viewer_html,
)


def _frames_json_from_html(html: str):
    """Pull the ``const frames = [...]`` JS literal back out of the page
    and JSON-decode it. The ``</``-escape we apply on embed must be
    reversed before parsing."""
    m = re.search(r"const frames = (.+?);\s*\n\s*const initialMolId",
                  html, re.DOTALL)
    assert m is not None, "frames literal not found"
    js_literal = m.group(1)
    json_str = js_literal.replace("<\\/", "</")
    return json.loads(json_str)


def test_viewer_with_molblock_embeds_frame_in_js():
    smiles = "CCO"
    molblock = "Mrv0541 12345\n\n  9  8\nM  END\n"
    html = render_viewer_html(smiles, molblock, "abc123")
    assert "CCO" in html
    parsed = _frames_json_from_html(html)
    assert len(parsed) == 1
    assert parsed[0]["smiles"] == "CCO"
    assert parsed[0]["molblock"] == molblock
    # No D&D drop-zone hint should appear when a frame is provided.
    assert "ファイルをここにドロップ" not in html


def test_viewer_without_frames_shows_drop_zone():
    html = render_viewer_html(frames=[])
    assert "ファイルをここにドロップ" in html
    parsed = _frames_json_from_html(html)
    assert parsed == []


def test_molblock_with_script_terminator_is_escaped():
    """An adversarial MolBlock containing ``</script>`` must not break
    out of the embedding tag."""
    nasty = "before</script><script>alert('xss')</script>after"
    html = render_viewer_html("CCO", nasty, "id1")
    # Raw substring must NOT appear in the HTML.
    assert "</script><script>alert" not in html
    # The escaped form must appear (inside the frames JSON literal).
    assert "<\\/script>" in html


def test_molblock_with_backticks_and_newlines_round_trip_safe():
    nasty = "line1\nline2`backtick`\n\\backslash\\\n\"quote\""
    html = render_viewer_html("CCO", nasty, "id1")
    parsed = _frames_json_from_html(html)
    assert parsed[0]["molblock"] == nasty


def test_not_found_html_has_japanese_message():
    html = render_not_found_html()
    assert "分子データが見つかりません" in html


def test_expired_html_mentions_retention_days():
    html = render_expired_html(7)
    assert "7 日" in html


def test_drop_zone_lists_supported_formats():
    html = render_viewer_html(frames=[])
    for fmt in ("pdb", "sdf", "mol2", "xyz", "cube"):
        assert fmt in html


def test_viewer_guards_against_missing_3dmol_cdn():
    """If the 3Dmol.js CDN fails to load, the IIFE must surface a
    Japanese error in the role=alert region instead of silently dying
    on a ReferenceError. We grep the embedded JS for the guard rather
    than running it: keeps the test hermetic (no browser)."""
    html = render_viewer_html("CCO", "M  END\n", "id1")
    assert "typeof $3Dmol === 'undefined'" in html
    assert "3D ビューアの読み込みに失敗しました" in html


def test_drop_zone_is_exposed_to_assistive_tech():
    html = render_viewer_html(frames=[])
    # The drop-zone is informational; it must NOT carry aria-hidden.
    assert 'id="dropzone"' in html
    assert 'id="dropzone" aria-hidden' not in html


def test_reset_view_restores_initial_camera():
    """3Dmol's zoomTo() only normalises distance — it leaves the user's
    rotation in place, so a bare zoomTo() does not actually reset the
    view. The viewer must snapshot getView() right after the model
    loads and use setView() on Reset to restore the full camera."""
    html = render_viewer_html("CCO", "M  END\n", "id1")
    assert "initialView = viewer.getView();" in html
    assert "viewer.setView(initialView)" in html
