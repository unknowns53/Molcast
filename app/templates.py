r"""HTML rendering for the viewer (see §7 of the design brief).

Two public entry points:

  * ``render_viewer_html(frames=..., mol_id=...)``    -> 3Dmol.js viewer
    page with a trajectory navigation strip (visible only when there
    are 2+ frames). The bare drop-zone page is the ``frames=[]`` case.
  * ``render_viewer_2d_html(frames=..., mol_id=...)`` -> static page of
    server-rendered SVGs (one per frame) with the same nav strip.
    Used by the ``/mol ... --no-3d`` flag.

Frame schema (set by :mod:`app.main._enrich_frame_meta`):

    {
        "kind":       "smiles" | "name",
        "input":      str,         # the user's original token
        "smiles":     str | None,
        "molblock":   str | None,  # 3D MolBlock (None for failed frames)
        "formula":    str | None,
        "mol_weight": float | None,
        "error":      str | None,
        "svg_text":   str | None,  # 2D only; populated by view_mol
    }

Backward-compatible kwargs ``smiles`` / ``molblock`` / ``formula`` /
``mol_weight`` are still accepted on ``render_viewer_html`` for the bare
drop-zone and existing single-mol callers — they get wrapped into a
single-element ``frames`` list internally.

The MolBlock is passed through ``json.dumps`` to embed it safely as a JS
string literal, and any ``</`` substring is escaped to ``<\/`` so that an
adversarial MolBlock containing ``</script>`` cannot break out of the
embedding ``<script>`` tag.
"""
from __future__ import annotations

import html
import json
from typing import Any


# 3Dmol.js: §15 lists https://3dmol.csb.pitt.edu/ as the project home;
# the CDN bundle is published at /build/3Dmol-min.js .
_THREEDMOL_CDN = "https://3dmol.csb.pitt.edu/build/3Dmol-min.js"


def _embed_js_string(value: str) -> str:
    """Encode ``value`` so it can sit inside an inline <script> as a JS
    string literal. ``json.dumps`` handles quotes, backslashes, and
    control chars; the ``</`` escape stops MolBlocks containing
    ``</script>`` from prematurely closing the host tag.
    """
    encoded = json.dumps(value, ensure_ascii=False)
    return encoded.replace("</", "<\\/")


def _embed_js_json(value: Any) -> str:
    """Like :func:`_embed_js_string` but for arbitrary JSON-serialisable
    values (lists / dicts). The same ``</`` escape applies so embedded
    strings cannot close the host ``<script>`` tag.
    """
    encoded = json.dumps(value, ensure_ascii=False)
    return encoded.replace("</", "<\\/")


_VIEWER_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; font-family: Arial, Helvetica, sans-serif; }
body { background: #fafafa; color: #222; }
.container { max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.25rem; margin: 0 0 8px 0; font-weight: 600; }
.controls { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; align-items: center; }
.controls button {
    min-height: 40px; min-width: 40px; padding: 6px 12px;
    border: 1px solid #bbb; border-radius: 4px; background: #fff;
    font-size: 0.9rem; cursor: pointer;
}
.controls button:hover { background: #f0f0f0; }
.controls button:disabled { opacity: 0.4; cursor: not-allowed; }
.controls button.copied { background: #e8f5e9; border-color: #81c784; color: #1b5e20; }
.controls button.toggled { background: #e3f2fd; border-color: #64b5f6; color: #0d47a1; }
/* Trajectory nav lives inline in the controls row (same flex line) so
   the page doesn't grow a third vertical band just for ◀ 1/3 ▶. The
   left border serves as a soft visual separator from the style /
   action buttons. */
.trajnav {
    display: inline-flex; align-items: center; gap: 6px;
    padding-left: 10px; margin-left: 4px;
    border-left: 1px solid #ddd;
    font-size: 0.9rem;
}
.trajnav[hidden] { display: none; }
.trajnav button {
    min-height: 40px; min-width: 40px; padding: 0 8px;
    border: 1px solid #bbb; border-radius: 4px; background: #fff;
    font-size: 1rem; cursor: pointer;
}
.trajnav button:hover { background: #f0f0f0; }
.trajnav button:disabled { opacity: 0.3; cursor: not-allowed; }
.trajnav .frame-indicator {
    min-width: 3.2rem; text-align: center;
    font-variant-numeric: tabular-nums; color: #555;
}
.trajnav .frame-status { font-size: 0.85rem; }
.trajnav .frame-status.ok { color: #1b5e20; }
.trajnav .frame-status.err { color: #c62828; }
#viewer {
    width: 100%; height: 80vh; position: relative;
    border: 1px solid #ddd; border-radius: 4px; background: #fff;
}
#dropzone {
    position: absolute; inset: 12px;
    border: 2px dashed #bbb; border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    color: #666; pointer-events: none; text-align: center; padding: 12px;
}
#dropzone.dragging { border-color: #4a90e2; background: rgba(74, 144, 226, 0.05); }
#error {
    margin-top: 8px; padding: 8px 12px; border-radius: 4px;
    background: #fdecea; color: #611a15; border: 1px solid #f5c2c0;
    display: none;
}
#error.show { display: block; }
/* In-viewer overlay (#6 — repositioned). Pinned to the upper-right of
   the viewer canvas so the meta sits over the model instead of
   stealing vertical space above the controls. Semi-transparent
   backdrop with a tiny shadow lifts it cleanly off the white
   viewer background. */
.meta-overlay {
    position: absolute; top: 10px; right: 10px; z-index: 5;
    background: rgba(255, 255, 255, 0.88);
    border: 1px solid #d8d8d8; border-radius: 6px;
    padding: 8px 12px;
    font-size: 0.85rem; color: #333;
    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.06);
    max-width: 45%;
    pointer-events: none;
}
.meta-overlay[hidden] { display: none; }
.meta-overlay .row { display: flex; align-items: baseline; gap: 8px; }
.meta-overlay .row + .row { margin-top: 2px; }
.meta-overlay .label {
    color: #999; font-weight: 600;
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.4px;
    min-width: 3.2rem;
}
.meta-overlay .value {
    font-family: ui-monospace, Menlo, Consolas, monospace; color: #222;
}
/* SMILES is variable-length; allow it to wrap inside the overlay
   rather than blowing the 45% max-width. Truncation kept off so
   short SMILES read cleanly; the Copy SMILES button is the canonical
   "give me the full string" affordance. */
.meta-overlay .value.smiles {
    word-break: break-all; overflow-wrap: anywhere;
}
.svg-stack { position: relative; min-height: 320px; }
.svg-2d {
    background: #fff; border: 1px solid #ddd; border-radius: 4px;
    padding: 16px; text-align: center;
}
.svg-2d[hidden] { display: none; }
.svg-2d svg { max-width: 100%; height: auto; }
.svg-2d .error-msg {
    color: #c62828; font-size: 0.95rem; padding: 24px;
}
.foot { margin-top: 8px; font-size: 0.8rem; color: #888; }
"""


def _normalise_frames(
    frames: list[dict[str, Any]] | None,
    *,
    smiles: str | None,
    molblock: str | None,
    formula: str | None,
    mol_weight: float | None,
) -> list[dict[str, Any]]:
    """Back-compat shim. ``frames=None`` plus the legacy single-mol
    kwargs builds a single-element list; ``frames=[]`` stays empty.
    """
    if frames is not None:
        return frames
    if molblock or smiles:
        return [
            {
                "kind": "smiles",
                "input": smiles or "",
                "smiles": smiles,
                "molblock": molblock,
                "error": None,
                "formula": formula,
                "mol_weight": mol_weight,
            }
        ]
    return []


def _frame_for_js(frame: dict[str, Any]) -> dict[str, Any]:
    """Strip a frame to the JS-side shape. Keeps the payload small
    (omits server-only fields) and stable across schema additions."""
    return {
        "kind": frame.get("kind") or "smiles",
        "input": frame.get("input") or "",
        "smiles": frame.get("smiles"),
        "molblock": frame.get("molblock"),
        "formula": frame.get("formula"),
        "mol_weight": frame.get("mol_weight"),
        "error": frame.get("error"),
    }


def render_viewer_html(
    smiles: str | None = None,
    molblock: str | None = None,
    mol_id: str | None = None,
    *,
    supported_formats: tuple[str, ...] = ("pdb", "sdf", "mol2", "xyz", "cube"),
    formula: str | None = None,
    mol_weight: float | None = None,
    frames: list[dict[str, Any]] | None = None,
) -> str:
    """3D viewer page. ``frames`` is the new path; the legacy positional
    ``smiles`` / ``molblock`` / ``mol_id`` form is wrapped into a one-
    element trajectory for backward compatibility.
    """
    use_frames = _normalise_frames(
        frames,
        smiles=smiles, molblock=molblock,
        formula=formula, mol_weight=mol_weight,
    )
    frames_js = _embed_js_json([_frame_for_js(f) for f in use_frames])
    mol_id_js = _embed_js_string(mol_id) if mol_id else "null"
    formats_js = json.dumps(list(supported_formats))

    drop_hint = (
        "ファイルをここにドロップ "
        f"({', '.join(supported_formats)})"
    )
    dropzone_html = (
        f'<div id="dropzone">{html.escape(drop_hint)}</div>'
        if not use_frames
        else ""
    )
    # Empty frames -> no nav, no overlay, no preloaded SMILES (drop-zone
    # case). The overlay element is always emitted; JS toggles ``hidden``.
    nav_hidden_attr = "" if len(use_frames) > 1 else "hidden"

    script = f"""
(function () {{
    const frames = {frames_js};
    const initialMolId = {mol_id_js};
    const supportedFormats = new Set({formats_js});
    const errorBox = document.getElementById('error');
    const dropzone = document.getElementById('dropzone');
    const viewerEl = document.getElementById('viewer');
    const metaOverlay = document.getElementById('meta-overlay');
    const metaFormula = document.getElementById('meta-formula-value');
    const metaMw = document.getElementById('meta-mw-value');
    const metaSmiles = document.getElementById('meta-smiles-value');
    const metaFormulaRow = document.getElementById('meta-formula-row');
    const metaMwRow = document.getElementById('meta-mw-row');
    const metaSmilesRow = document.getElementById('meta-smiles-row');
    const nav = document.getElementById('trajnav');
    const navPrev = document.getElementById('btn-prev');
    const navNext = document.getElementById('btn-next');
    const navInd = document.getElementById('frame-indicator');
    const navStatus = document.getElementById('frame-status');

    function showError(msg) {{
        errorBox.textContent = msg;
        errorBox.classList.add('show');
    }}
    function clearError() {{
        errorBox.textContent = '';
        errorBox.classList.remove('show');
    }}

    // CPK colors for atom labels (#4). Hand-picked subset matching the
    // 3Dmol.js sphere colors so the label text echoes the sphere.
    const CPK = {{
        H:'#909090', C:'#303030', N:'#3050F8', O:'#FF0D0D',
        F:'#90E050', P:'#FF8000', S:'#B8B800', Cl:'#1FA01F',
        Br:'#A62929', I:'#940094',
        Na:'#AB5CF2', K:'#8F40D4', Li:'#CC80FF', Mg:'#8AFF00',
        Ca:'#3DFF00', B:'#FFB5B5', Si:'#F0C8A0', Fe:'#E06633',
    }};
    function cpkOf(elem) {{ return CPK[elem] || '#303030'; }}

    // Guard against the 3Dmol.js CDN failing to load (lab proxy, offline
    // dev, transient CDN 5xx). Without this, the very first call to
    // $3Dmol.createViewer would throw ReferenceError and silently leave
    // the buttons and drop-zone unwired with no message to the user.
    if (typeof $3Dmol === 'undefined' || !$3Dmol.createViewer) {{
        showError('3D ビューアの読み込みに失敗しました。ネットワーク接続をご確認のうえ、ページを再読み込みしてください。');
        return;
    }}

    let viewer;
    try {{
        viewer = $3Dmol.createViewer('viewer', {{ backgroundColor: 'white' }});
    }} catch (e) {{
        showError('3D ビューアの初期化に失敗しました。ブラウザを更新してください。');
        return;
    }}
    let initialView = null;
    let currentFrameIdx = 0;
    // Default style: slightly thicker H spheres (#4). 0.30 reads more
    // naturally than 0.25 at the typical Web zoom level — the H atoms
    // were almost invisible at 0.25.
    const DEFAULT_STYLE = {{ stick: {{ radius: 0.15 }}, sphere: {{ scale: 0.30 }} }};

    // ---- Label state (toggle shared across frames) ----------------------
    let labelsOn = false;
    let labelsIncludeH = false;
    const labelHandles = [];
    function renderLabels() {{
        const m = viewer.getModel();
        if (!m) return;
        m.selectedAtoms({{}}).forEach(function (a) {{
            if (!labelsIncludeH && a.elem === 'H') return;
            const lbl = viewer.addLabel(a.elem, {{
                position: {{ x: a.x, y: a.y, z: a.z }},
                font: 'Arial',
                fontSize: 14,
                fontColor: cpkOf(a.elem),
                backgroundColor: 'white',
                backgroundOpacity: 0.85,
                borderThickness: 0,
                inFront: true,
                alignment: 'center',
            }});
            labelHandles.push(lbl);
        }});
    }}
    function clearLabels() {{
        labelHandles.forEach(function (l) {{ viewer.removeLabel(l); }});
        labelHandles.length = 0;
    }}
    function updateLabelButton() {{
        if (!btnLabels) return;
        // Static "Labels" text; the .toggled CSS class is the visual
        // on/off cue. The title attribute carries the H-inclusion hint.
        if (labelsOn) {{
            btnLabels.classList.add('toggled');
            btnLabels.title = labelsIncludeH
                ? 'Labels on (H included). Shift+click to hide H.'
                : 'Labels on (heavy atoms). Shift+click to include H.';
        }} else {{
            btnLabels.classList.remove('toggled');
            btnLabels.title = 'Click to show atom labels. Shift+click to include H.';
        }}
    }}
    function toggleLabels(force, includeH) {{
        const want = (force === undefined) ? !labelsOn : Boolean(force);
        const wantH = (includeH === undefined) ? labelsIncludeH : Boolean(includeH);
        if (want === labelsOn && wantH === labelsIncludeH) return;
        clearLabels();
        labelsOn = want;
        labelsIncludeH = wantH;
        if (labelsOn) renderLabels();
        updateLabelButton();
        viewer.render();
    }}

    // ---- Rotate state ---------------------------------------------------
    let rotateOn = false;
    function toggleRotate(force) {{
        const want = (force === undefined) ? !rotateOn : Boolean(force);
        if (want === rotateOn) return;
        rotateOn = want;
        if (rotateOn) {{
            viewer.spin('y', 1);
            btnRotate.classList.add('toggled');
            btnRotate.title = 'Stop auto-rotate';
        }} else {{
            viewer.spin(false);
            btnRotate.classList.remove('toggled');
            btnRotate.title = 'Start auto-rotate';
        }}
    }}

    // ---- Frame loader ---------------------------------------------------
    function updateMeta(f) {{
        let anyMeta = false;
        if (f && f.formula) {{
            metaFormula.textContent = f.formula;
            metaFormulaRow.hidden = false;
            anyMeta = true;
        }} else {{
            metaFormulaRow.hidden = true;
        }}
        if (f && (f.mol_weight !== null && f.mol_weight !== undefined)) {{
            metaMw.textContent = Number(f.mol_weight).toFixed(2);
            metaMwRow.hidden = false;
            anyMeta = true;
        }} else {{
            metaMwRow.hidden = true;
        }}
        // SMILES lives in the overlay too (it's the single source of
        // truth now that the top bar is gone). Use the input string as
        // a fallback display when the SMILES is missing (name failed
        // to resolve, etc.).
        const smiText = (f && f.smiles) ? f.smiles : (f && f.input) ? f.input : '';
        if (smiText) {{
            metaSmiles.textContent = smiText;
            metaSmilesRow.hidden = false;
            anyMeta = true;
        }} else {{
            metaSmilesRow.hidden = true;
        }}
        metaOverlay.hidden = !anyMeta;
    }}
    function escapeHtml(s) {{
        return String(s).replace(/[&<>"']/g, function (c) {{
            return ({{
                '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
            }})[c];
        }});
    }}

    function updateNavUI() {{
        if (!nav) return;
        if (frames.length <= 1) {{
            nav.hidden = true;
            return;
        }}
        nav.hidden = false;
        navInd.textContent = (currentFrameIdx + 1) + ' / ' + frames.length;
        navPrev.disabled = currentFrameIdx <= 0;
        navNext.disabled = currentFrameIdx >= frames.length - 1;
        const f = frames[currentFrameIdx];
        if (f && f.error) {{
            navStatus.textContent = '✗';
            navStatus.className = 'frame-status err';
            navStatus.title = f.error;
        }} else {{
            navStatus.textContent = '✓';
            navStatus.className = 'frame-status ok';
            navStatus.title = (f && f.input) ? f.input : '';
        }}
    }}

    function loadFrame(i) {{
        if (i < 0 || i >= frames.length) return;
        currentFrameIdx = i;
        const f = frames[i];
        clearLabels();  // labels are positions; re-render after model swap
        updateMeta(f);
        updateNavUI();
        if (!f) {{ return; }}
        if (f.error || !f.molblock) {{
            viewer.clear();
            viewer.render();
            initialView = null;
            showError(f.error || 'このフレームには 3D 構造がありません。');
            return;
        }}
        try {{
            viewer.clear();
            viewer.addModel(f.molblock, 'mol');
            viewer.setStyle({{}}, DEFAULT_STYLE);
            viewer.zoomTo();
            viewer.render();
            initialView = viewer.getView();
            if (dropzone) dropzone.style.display = 'none';
            clearError();
            if (labelsOn) {{ renderLabels(); viewer.render(); }}
        }} catch (e) {{
            showError('分子データの解釈に失敗しました。SMILES から再生成してください。');
        }}
    }}

    if (frames.length > 0) {{
        loadFrame(0);
    }} else {{
        updateMeta(null);
        updateNavUI();
    }}

    // ---- Drag-and-drop (drop-zone) -------------------------------------
    function handleFile(file) {{
        if (!file) return;
        const ext = (file.name.split('.').pop() || '').toLowerCase();
        if (!supportedFormats.has(ext)) {{
            showError('ファイル形式に対応していません。pdb / sdf / mol2 / xyz / cube または OpenBabel で変換してください。');
            return;
        }}
        const reader = new FileReader();
        reader.onload = function () {{
            try {{
                viewer.clear();
                viewer.addModel(reader.result, ext);
                viewer.setStyle({{}}, DEFAULT_STYLE);
                viewer.zoomTo();
                viewer.render();
                initialView = viewer.getView();
                if (dropzone) dropzone.style.display = 'none';
                clearError();
            }} catch (e) {{
                showError('分子データの解釈に失敗しました。');
            }}
        }};
        reader.onerror = function () {{ showError('ファイル読み込みに失敗しました。'); }};
        reader.readAsText(file);
    }}
    viewerEl.addEventListener('dragover', function (e) {{
        e.preventDefault();
        if (dropzone) dropzone.classList.add('dragging');
    }});
    viewerEl.addEventListener('dragleave', function () {{
        if (dropzone) dropzone.classList.remove('dragging');
    }});
    viewerEl.addEventListener('drop', function (e) {{
        e.preventDefault();
        if (dropzone) dropzone.classList.remove('dragging');
        const file = e.dataTransfer.files[0];
        handleFile(file);
    }});

    // ---- Style buttons --------------------------------------------------
    document.getElementById('btn-stick').addEventListener('click', function () {{
        viewer.setStyle({{}}, {{ stick: {{}} }}); viewer.render();
        if (labelsOn) {{ clearLabels(); renderLabels(); viewer.render(); }}
    }});
    document.getElementById('btn-bs').addEventListener('click', function () {{
        viewer.setStyle({{}}, DEFAULT_STYLE);
        viewer.render();
        if (labelsOn) {{ clearLabels(); renderLabels(); viewer.render(); }}
    }});
    document.getElementById('btn-sphere').addEventListener('click', function () {{
        viewer.setStyle({{}}, {{ sphere: {{}} }}); viewer.render();
        if (labelsOn) {{ clearLabels(); renderLabels(); viewer.render(); }}
    }});
    document.getElementById('btn-reset').addEventListener('click', function () {{
        if (initialView) {{ viewer.setView(initialView); }}
        else {{ viewer.zoomTo(); }}
        viewer.render();
    }});

    // ---- Label toggle ---------------------------------------------------
    const btnLabels = document.getElementById('btn-labels');
    if (btnLabels) {{
        btnLabels.addEventListener('click', function (ev) {{
            if (ev.shiftKey) {{
                toggleLabels(true, !labelsIncludeH);
            }} else {{
                toggleLabels(undefined, false);
            }}
        }});
        updateLabelButton();  // seed the title attribute
    }}

    // ---- Rotate toggle --------------------------------------------------
    const btnRotate = document.getElementById('btn-rotate');
    if (btnRotate) {{
        btnRotate.addEventListener('click', function () {{ toggleRotate(); }});
        btnRotate.title = 'Start auto-rotate';
    }}

    // ---- SMILES copy (#7) -----------------------------------------------
    const btnCopy = document.getElementById('btn-copy');
    if (btnCopy) {{
        btnCopy.addEventListener('click', function () {{
            const f = frames[currentFrameIdx];
            const text = (f && f.smiles) ? f.smiles : '';
            if (!text) {{
                showError('現在のフレームに SMILES がありません。');
                return;
            }}
            const fallback = function () {{
                const ta = document.createElement('textarea');
                ta.value = text; ta.setAttribute('readonly', '');
                ta.style.position = 'absolute'; ta.style.left = '-9999px';
                document.body.appendChild(ta);
                ta.select();
                try {{ document.execCommand('copy'); }} catch (e) {{}}
                document.body.removeChild(ta);
            }};
            const onOk = function () {{
                btnCopy.classList.add('copied');
                const orig = btnCopy.textContent;
                btnCopy.textContent = 'コピー済み';
                setTimeout(function () {{
                    btnCopy.classList.remove('copied');
                    btnCopy.textContent = orig;
                }}, 1500);
            }};
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(text).then(onOk, function () {{
                    fallback(); onOk();
                }});
            }} else {{
                fallback(); onOk();
            }}
        }});
    }}

    // ---- PNG ダウンロード (#7) -----------------------------------------
    const btnPng = document.getElementById('btn-save-png');
    if (btnPng) {{
        btnPng.addEventListener('click', function () {{
            try {{
                const dataUrl = viewer.pngURI();
                const a = document.createElement('a');
                a.href = dataUrl;
                const tag = (initialMolId || 'viewer') + '-' + (currentFrameIdx + 1);
                a.download = 'molcast-' + tag + '.png';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }} catch (e) {{
                showError('画像の保存に失敗しました。');
            }}
        }});
    }}

    // ---- Trajectory navigation -----------------------------------------
    if (navPrev) navPrev.addEventListener('click', function () {{
        if (currentFrameIdx > 0) loadFrame(currentFrameIdx - 1);
    }});
    if (navNext) navNext.addEventListener('click', function () {{
        if (currentFrameIdx < frames.length - 1) loadFrame(currentFrameIdx + 1);
    }});
    document.addEventListener('keydown', function (ev) {{
        // Don't hijack arrow keys when the user is typing in an input.
        const tgt = ev.target;
        if (tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA')) return;
        if (ev.key === 'ArrowLeft') {{
            if (currentFrameIdx > 0) {{ loadFrame(currentFrameIdx - 1); ev.preventDefault(); }}
        }} else if (ev.key === 'ArrowRight') {{
            if (currentFrameIdx < frames.length - 1) {{ loadFrame(currentFrameIdx + 1); ev.preventDefault(); }}
        }}
    }});

    // ---- Honor URL query params after the initial model has loaded ----
    if (frames.length > 0) {{
        try {{
            const params = new URLSearchParams(window.location.search);
            if (params.get('label') === '1') toggleLabels(true);
            if (params.get('rotate') === '1') toggleRotate(true);
            const frameQ = parseInt(params.get('frame') || '', 10);
            if (!isNaN(frameQ) && frameQ >= 1 && frameQ <= frames.length) {{
                loadFrame(frameQ - 1);
            }}
        }} catch (e) {{ /* legacy browsers */ }}
    }}
}})();
"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Molecule Viewer</title>
<style>{_VIEWER_CSS}</style>
<script src="{_THREEDMOL_CDN}"></script>
</head>
<body>
<div class="container">
  <h1>Molecule Viewer</h1>
  <div class="controls">
    <button id="btn-stick" type="button">Stick</button>
    <button id="btn-bs" type="button">Ball &amp; Stick</button>
    <button id="btn-sphere" type="button">Sphere</button>
    <button id="btn-reset" type="button">Reset</button>
    <button id="btn-labels" type="button">Labels</button>
    <button id="btn-rotate" type="button">Rotate</button>
    <button id="btn-copy" type="button">Copy SMILES</button>
    <button id="btn-save-png" type="button">Save PNG</button>
    <span class="trajnav" id="trajnav" {nav_hidden_attr}>
      <button id="btn-prev" type="button" aria-label="prev frame">◀</button>
      <span class="frame-indicator" id="frame-indicator">1 / 1</span>
      <button id="btn-next" type="button" aria-label="next frame">▶</button>
      <span class="frame-status ok" id="frame-status">✓</span>
    </span>
  </div>
  <div id="viewer">{dropzone_html}<div class="meta-overlay" id="meta-overlay" aria-label="分子情報" hidden>
    <div class="row" id="meta-formula-row" hidden><span class="label">分子式</span><span class="value" id="meta-formula-value"></span></div>
    <div class="row" id="meta-mw-row" hidden><span class="label">分子量</span><span class="value" id="meta-mw-value"></span></div>
    <div class="row" id="meta-smiles-row" hidden><span class="label">SMILES</span><span class="value smiles" id="meta-smiles-value"></span></div>
  </div></div>
  <div id="error" role="alert"></div>
  <div class="foot">3Dmol.js / RDKit</div>
</div>
<script>{script}</script>
</body>
</html>"""


def render_viewer_2d_html(
    *,
    frames: list[dict[str, Any]] | None = None,
    mol_id: str | None = None,
    # Back-compat single-mol kwargs (used by the previous --no-3d path).
    smiles: str | None = None,
    svg_text: str | None = None,
    formula: str | None = None,
    mol_weight: float | None = None,
) -> str:
    """2D static depiction page. Each frame's ``svg_text`` is server-
    rendered (see :func:`app.rdkit_utils.molblock_to_svg`) and embedded
    inline; the page renders one SVG at a time and uses the same nav
    strip as the 3D viewer.
    """
    if frames is None:
        frames = [
            {
                "kind": "smiles", "input": smiles or "",
                "smiles": smiles, "molblock": None,
                "formula": formula, "mol_weight": mol_weight,
                "svg_text": svg_text,
                "error": None,
            }
        ]
    nav_hidden_attr = "" if len(frames) > 1 else "hidden"

    # Build the SVG stack server-side; JS only flips visibility on nav.
    svg_blocks: list[str] = []
    for i, f in enumerate(frames):
        hidden_attr = "" if i == 0 else "hidden"
        if f.get("svg_text"):
            cleaned = f["svg_text"]
            if cleaned.startswith("<?xml"):
                end = cleaned.find("?>")
                if end != -1:
                    cleaned = cleaned[end + 2 :].lstrip()
            inner = cleaned
        elif f.get("error"):
            inner = (
                f'<div class="error-msg">{html.escape(f["error"])}</div>'
            )
        else:
            inner = (
                '<div class="error-msg">2D 構造の生成に失敗しました。</div>'
            )
        svg_blocks.append(
            f'<div class="svg-2d" data-frame="{i}" {hidden_attr} role="img" '
            f'aria-label="frame {i + 1}">{inner}</div>'
        )

    frames_js = _embed_js_json([_frame_for_js(f) for f in frames])
    mol_id_js = _embed_js_string(mol_id) if mol_id else "null"

    script = f"""
(function () {{
    const frames = {frames_js};
    const initialMolId = {mol_id_js};
    let currentFrameIdx = 0;
    const stack = document.getElementById('svg-stack');
    const blocks = stack ? stack.querySelectorAll('.svg-2d') : [];
    const nav = document.getElementById('trajnav');
    const navPrev = document.getElementById('btn-prev');
    const navNext = document.getElementById('btn-next');
    const navInd = document.getElementById('frame-indicator');
    const navStatus = document.getElementById('frame-status');
    const metaOverlay = document.getElementById('meta-overlay');
    const metaFormula = document.getElementById('meta-formula-value');
    const metaMw = document.getElementById('meta-mw-value');
    const metaSmiles = document.getElementById('meta-smiles-value');
    const metaFormulaRow = document.getElementById('meta-formula-row');
    const metaMwRow = document.getElementById('meta-mw-row');
    const metaSmilesRow = document.getElementById('meta-smiles-row');

    function updateMeta(f) {{
        let anyMeta = false;
        if (metaFormula && f && f.formula) {{
            metaFormula.textContent = f.formula;
            metaFormulaRow.hidden = false; anyMeta = true;
        }} else if (metaFormulaRow) {{ metaFormulaRow.hidden = true; }}
        if (metaMw && f && (f.mol_weight !== null && f.mol_weight !== undefined)) {{
            metaMw.textContent = Number(f.mol_weight).toFixed(2);
            metaMwRow.hidden = false; anyMeta = true;
        }} else if (metaMwRow) {{ metaMwRow.hidden = true; }}
        const smiText = (f && f.smiles) ? f.smiles : (f && f.input) ? f.input : '';
        if (metaSmiles && smiText) {{
            metaSmiles.textContent = smiText;
            metaSmilesRow.hidden = false; anyMeta = true;
        }} else if (metaSmilesRow) {{ metaSmilesRow.hidden = true; }}
        if (metaOverlay) metaOverlay.hidden = !anyMeta;
    }}

    function updateNavUI() {{
        if (!nav) return;
        if (frames.length <= 1) {{ nav.hidden = true; return; }}
        nav.hidden = false;
        navInd.textContent = (currentFrameIdx + 1) + ' / ' + frames.length;
        navPrev.disabled = currentFrameIdx <= 0;
        navNext.disabled = currentFrameIdx >= frames.length - 1;
        const f = frames[currentFrameIdx];
        if (f && f.error) {{
            navStatus.textContent = '✗';
            navStatus.className = 'frame-status err';
            navStatus.title = f.error;
        }} else {{
            navStatus.textContent = '✓';
            navStatus.className = 'frame-status ok';
            navStatus.title = (f && f.input) ? f.input : '';
        }}
    }}

    function showFrame(i) {{
        if (i < 0 || i >= frames.length) return;
        currentFrameIdx = i;
        blocks.forEach(function (b) {{
            const idx = parseInt(b.getAttribute('data-frame'), 10);
            b.hidden = (idx !== i);
        }});
        updateMeta(frames[i]);
        updateNavUI();
    }}

    if (frames.length > 0) showFrame(0);

    if (navPrev) navPrev.addEventListener('click', function () {{
        if (currentFrameIdx > 0) showFrame(currentFrameIdx - 1);
    }});
    if (navNext) navNext.addEventListener('click', function () {{
        if (currentFrameIdx < frames.length - 1) showFrame(currentFrameIdx + 1);
    }});
    document.addEventListener('keydown', function (ev) {{
        const tgt = ev.target;
        if (tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA')) return;
        if (ev.key === 'ArrowLeft' && currentFrameIdx > 0) {{
            showFrame(currentFrameIdx - 1); ev.preventDefault();
        }} else if (ev.key === 'ArrowRight' && currentFrameIdx < frames.length - 1) {{
            showFrame(currentFrameIdx + 1); ev.preventDefault();
        }}
    }});

    // SMILES copy button (#7).
    const btnCopy = document.getElementById('btn-copy');
    if (btnCopy) {{
        btnCopy.addEventListener('click', function () {{
            const f = frames[currentFrameIdx];
            const text = (f && f.smiles) ? f.smiles : '';
            if (!text) return;
            const fallback = function () {{
                const ta = document.createElement('textarea');
                ta.value = text; ta.setAttribute('readonly', '');
                ta.style.position = 'absolute'; ta.style.left = '-9999px';
                document.body.appendChild(ta);
                ta.select();
                try {{ document.execCommand('copy'); }} catch (e) {{}}
                document.body.removeChild(ta);
            }};
            const onOk = function () {{
                btnCopy.classList.add('copied');
                const orig = btnCopy.textContent;
                btnCopy.textContent = 'Copied';
                setTimeout(function () {{
                    btnCopy.classList.remove('copied');
                    btnCopy.textContent = orig;
                }}, 1500);
            }};
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(text).then(onOk, function () {{
                    fallback(); onOk();
                }});
            }} else {{
                fallback(); onOk();
            }}
        }});
    }}
    void initialMolId;  // reserved for future filename use
}})();
"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Molecule Viewer (2D)</title>
<style>{_VIEWER_CSS}</style>
</head>
<body>
<div class="container">
  <h1>Molecule Viewer (2D)</h1>
  <div class="controls">
    <button id="btn-copy" type="button">Copy SMILES</button>
    <span class="trajnav" id="trajnav" {nav_hidden_attr}>
      <button id="btn-prev" type="button" aria-label="prev frame">◀</button>
      <span class="frame-indicator" id="frame-indicator">1 / 1</span>
      <button id="btn-next" type="button" aria-label="next frame">▶</button>
      <span class="frame-status ok" id="frame-status">✓</span>
    </span>
  </div>
  <div class="meta-overlay" id="meta-overlay" aria-label="分子情報" hidden style="position: static; max-width: none; margin-bottom: 8px;">
    <div class="row" id="meta-formula-row" hidden><span class="label">分子式</span><span class="value" id="meta-formula-value"></span></div>
    <div class="row" id="meta-mw-row" hidden><span class="label">分子量</span><span class="value" id="meta-mw-value"></span></div>
    <div class="row" id="meta-smiles-row" hidden><span class="label">SMILES</span><span class="value smiles" id="meta-smiles-value"></span></div>
  </div>
  <div class="svg-stack" id="svg-stack">{''.join(svg_blocks)}</div>
  <div class="foot">RDKit MolDraw2DSVG</div>
</div>
<script>{script}</script>
</body>
</html>"""


def render_not_found_html() -> str:
    return _render_error_page(
        title="Molecule Viewer",
        heading="分子データが見つかりません",
        body="URL をご確認ください。",
        status_hint="404",
    )


def render_expired_html(retention_days: int) -> str:
    return _render_error_page(
        title="Molecule Viewer",
        heading="この 3D ビューアは期限切れです",
        body=(
            f"発行から {retention_days} 日が経過しました。"
            "Slack で <code>/mol</code> を実行して再生成してください。"
        ),
        status_hint="410",
    )


def _render_error_page(*, title: str, heading: str, body: str, status_hint: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
html, body {{ margin: 0; padding: 0; font-family: Arial, Helvetica, sans-serif; background: #fafafa; color: #222; }}
.box {{ max-width: 640px; margin: 12vh auto; padding: 24px;
        background: #fff; border: 1px solid #ddd; border-radius: 6px; }}
h1 {{ font-size: 1.25rem; margin: 0 0 12px 0; }}
.body {{ font-size: 0.95rem; line-height: 1.6; color: #444; }}
code {{ font-family: ui-monospace, Menlo, Consolas, monospace;
        background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }}
.hint {{ font-size: 0.8rem; color: #888; margin-top: 16px; }}
</style>
</head>
<body>
<div class="box" role="alert">
  <h1>{html.escape(heading)}</h1>
  <div class="body">{body}</div>
  <div class="hint">[{html.escape(status_hint)}]</div>
</div>
</body>
</html>"""
