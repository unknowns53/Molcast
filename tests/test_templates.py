"""Tests for §7 (viewer HTML rendering and JS embedding safety)."""
from __future__ import annotations

import json
import re

from app.templates import (
    render_expired_html,
    render_not_found_html,
    render_viewer_html,
)


def test_viewer_with_molblock_includes_smiles_and_data():
    smiles = "CCO"
    molblock = "Mrv0541 12345\n\n  9  8\nM  END\n"
    html = render_viewer_html(smiles, molblock, "abc123")
    assert "CCO" in html
    # The MolBlock string should appear as a JS string literal (json-encoded).
    encoded = json.dumps(molblock)
    encoded_safe = encoded.replace("</", "<\\/")
    assert encoded_safe in html
    # No D&D drop-zone hint should appear when molblock is provided.
    assert "ファイルをここにドロップ" not in html


def test_viewer_without_molblock_shows_drop_zone():
    html = render_viewer_html(None, None, None)
    assert "ファイルをここにドロップ" in html
    # The script should still initialise but with no preloaded model.
    assert "const initialMolblock = null;" in html


def test_molblock_with_script_terminator_is_escaped():
    """An adversarial MolBlock containing ``</script>`` must not break
    out of the embedding tag."""
    nasty = "before</script><script>alert('xss')</script>after"
    html = render_viewer_html("CCO", nasty, "id1")
    # Raw substring must NOT appear in the HTML.
    assert "</script><script>alert" not in html
    # The escaped form must appear.
    assert "<\\/script>" in html


def test_molblock_with_backticks_and_newlines_round_trip_safe():
    nasty = "line1\nline2`backtick`\n\\backslash\\\n\"quote\""
    html = render_viewer_html("CCO", nasty, "id1")
    # The embedded JS literal must be a valid JSON string (after un-doing
    # the </ escape) that decodes back to the original MolBlock.
    m = re.search(r"const initialMolblock = (.+?);", html, re.DOTALL)
    assert m is not None
    js_literal = m.group(1)
    # Reverse the </-escape before JSON parsing.
    json_str = js_literal.replace("<\\/", "</")
    assert json.loads(json_str) == nasty


def test_not_found_html_has_japanese_message():
    html = render_not_found_html()
    assert "分子データが見つかりません" in html


def test_expired_html_mentions_retention_days():
    html = render_expired_html(7)
    assert "7 日" in html


def test_drop_zone_lists_supported_formats():
    html = render_viewer_html(None, None, None)
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
    html = render_viewer_html(None, None, None)
    # The drop-zone is informational; it must NOT carry aria-hidden.
    assert 'id="dropzone"' in html
    # The whole rendered HTML must contain no aria-hidden on the dropzone.
    assert 'id="dropzone" aria-hidden' not in html


def test_reset_view_restores_initial_camera():
    """3Dmol's zoomTo() only normalises distance — it leaves the user's
    rotation in place, so a bare zoomTo() does not actually reset the
    view. The viewer must snapshot getView() right after the model
    loads and use setView() on Reset to restore the full camera."""
    html = render_viewer_html("CCO", "M  END\n", "id1")
    # Snapshot is taken after the initial render.
    assert "initialView = viewer.getView();" in html
    # Reset handler uses setView, not a bare zoomTo.
    assert "viewer.setView(initialView)" in html
