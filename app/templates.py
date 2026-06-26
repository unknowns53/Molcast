r"""HTML rendering for the viewer (see §7 of the design brief).

Two public entry points:

  * ``render_viewer_html``       -> 3Dmol.js 3D viewer page. Used by
    ``/view/{id}`` (default) and by the bare drop-zone page at
    ``/view/`` / ``/``.
  * ``render_viewer_2d_html``    -> static 2D depiction page for the
    ``/mol ... --no-3d`` flag (#3). Skips 3Dmol.js entirely and embeds
    a server-rendered SVG from ``rdkit_utils.molblock_to_svg``.

Both pages can show optional metadata (formula / MW / SMILES) when the
caller supplies it.

The MolBlock is passed through ``json.dumps`` to embed it safely as a JS
string literal, and any ``</`` substring is escaped to ``<\/`` so that an
adversarial MolBlock containing ``</script>`` cannot break out of the
embedding ``<script>`` tag.
"""
from __future__ import annotations

import html
import json


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


_VIEWER_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; font-family: Arial, Helvetica, sans-serif; }
body { background: #fafafa; color: #222; }
.container { max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.25rem; margin: 0 0 8px 0; font-weight: 600; }
.smiles {
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.9rem;
    background: #fff; border: 1px solid #ddd; border-radius: 4px;
    padding: 6px 8px; word-break: break-all; margin-bottom: 8px;
}
.meta {
    display: flex; flex-wrap: wrap; gap: 6px 12px; margin-bottom: 8px;
    font-size: 0.85rem; color: #444;
}
.meta-item {
    background: #fff; border: 1px solid #ddd; border-radius: 4px;
    padding: 4px 8px;
}
.meta-item .label { color: #888; font-weight: 600; margin-right: 4px; }
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
.controls { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.controls button {
    min-height: 44px; min-width: 44px; padding: 8px 14px;
    border: 1px solid #bbb; border-radius: 4px; background: #fff;
    font-size: 0.95rem; cursor: pointer;
}
.controls button:hover { background: #f0f0f0; }
.controls button.copied { background: #e8f5e9; border-color: #81c784; color: #1b5e20; }
.controls button.toggled { background: #e3f2fd; border-color: #64b5f6; color: #0d47a1; }
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
.svg-2d {
    background: #fff; border: 1px solid #ddd; border-radius: 4px;
    padding: 16px; text-align: center;
}
.svg-2d svg { max-width: 100%; height: auto; }
.foot { margin-top: 8px; font-size: 0.8rem; color: #888; }
"""


def _render_meta_block(
    *,
    smiles: str | None,
    formula: str | None,
    mol_weight: float | None,
) -> str:
    """Inline metadata pills (used by the 2D static page #3 --no-3d).

    SMILES is rendered separately in the ``.smiles`` block above; here we
    surface only formula / molecular weight so the two blocks don't
    repeat the same fact.
    """
    items: list[str] = []
    if formula:
        items.append(
            f'<div class="meta-item"><span class="label">分子式</span>'
            f'{html.escape(formula)}</div>'
        )
    if mol_weight is not None:
        items.append(
            f'<div class="meta-item"><span class="label">分子量</span>'
            f'{mol_weight:.2f}</div>'
        )
    if not items:
        return ""
    return f'<div class="meta">{"".join(items)}</div>'


def _render_meta_overlay(
    *,
    formula: str | None,
    mol_weight: float | None,
) -> str:
    """In-viewer overlay shown in the upper-right corner of the 3D canvas.

    Placed inside ``#viewer`` (which is ``position: relative``) so the
    formula / MW float over the model without consuming vertical space
    above the controls. Empty when both fields are missing.
    """
    rows: list[str] = []
    if formula:
        rows.append(
            '<div class="row">'
            '<span class="label">分子式</span>'
            f'<span class="value">{html.escape(formula)}</span>'
            '</div>'
        )
    if mol_weight is not None:
        rows.append(
            '<div class="row">'
            '<span class="label">分子量</span>'
            f'<span class="value">{mol_weight:.2f}</span>'
            '</div>'
        )
    if not rows:
        return ""
    return f'<div class="meta-overlay" aria-label="分子情報">{"".join(rows)}</div>'


def render_viewer_html(
    smiles: str | None,
    molblock: str | None,
    mol_id: str | None,
    *,
    supported_formats: tuple[str, ...] = ("pdb", "sdf", "mol2", "xyz", "cube"),
    formula: str | None = None,
    mol_weight: float | None = None,
) -> str:
    smiles_block = ""
    if smiles:
        smiles_block = (
            '<div class="smiles" aria-label="SMILES">'
            f"<strong>SMILES:</strong> {html.escape(smiles)}"
            "</div>"
        )

    meta_overlay = _render_meta_overlay(
        formula=formula, mol_weight=mol_weight
    )

    molblock_js = _embed_js_string(molblock) if molblock else "null"
    smiles_js = _embed_js_string(smiles) if smiles else "null"
    mol_id_js = _embed_js_string(mol_id) if mol_id else "null"
    formats_js = json.dumps(list(supported_formats))

    drop_hint = (
        "ファイルをここにドロップ "
        f"({', '.join(supported_formats)})"
    )

    script = f"""
(function () {{
    const initialMolblock = {molblock_js};
    const initialSmiles = {smiles_js};
    const initialMolId = {mol_id_js};
    const supportedFormats = new Set({formats_js});
    const errorBox = document.getElementById('error');
    const dropzone = document.getElementById('dropzone');
    const viewerEl = document.getElementById('viewer');

    // CPK colors for atom labels (#4). Hand-picked subset matching the
    // 3Dmol.js sphere colors so the label text echoes the sphere — gives
    // a quick visual key without needing a separate legend. Unknown
    // elements fall back to dark gray; H is filtered out before this
    // ever runs (see toggleLabels below).
    const CPK = {{
        H:'#909090', C:'#303030', N:'#3050F8', O:'#FF0D0D',
        F:'#90E050', P:'#FF8000', S:'#B8B800', Cl:'#1FA01F',
        Br:'#A62929', I:'#940094',
        Na:'#AB5CF2', K:'#8F40D4', Li:'#CC80FF', Mg:'#8AFF00',
        Ca:'#3DFF00', B:'#FFB5B5', Si:'#F0C8A0', Fe:'#E06633',
    }};
    function cpkOf(elem) {{ return CPK[elem] || '#303030'; }}

    function showError(msg) {{
        errorBox.textContent = msg;
        errorBox.classList.add('show');
    }}
    function clearError() {{
        errorBox.textContent = '';
        errorBox.classList.remove('show');
    }}

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
    let modelLoaded = false;
    // Snapshot of the camera right after the model loads. We can't just
    // call zoomTo() on Reset because 3Dmol's zoomTo() only normalises
    // distance — it leaves the user's rotation in place. setView()/
    // getView() bundle position + rotation + zoom so a round-trip
    // through them fully restores the initial framing.
    let initialView = null;
    // Default style: slightly thicker H spheres (#4). 0.30 reads more
    // naturally than 0.25 at the typical Web zoom level — the H atoms
    // were almost invisible at 0.25.
    const DEFAULT_STYLE = {{ stick: {{ radius: 0.15 }}, sphere: {{ scale: 0.30 }} }};

    function loadModel(text, fmt) {{
        try {{
            viewer.clear();
            viewer.addModel(text, fmt);
            viewer.setStyle({{}}, DEFAULT_STYLE);
            viewer.zoomTo();
            viewer.render();
            initialView = viewer.getView();
            modelLoaded = true;
            if (dropzone) dropzone.style.display = 'none';
            clearError();
        }} catch (e) {{
            showError('分子データの解釈に失敗しました。SMILES から再生成してください。');
        }}
    }}

    if (initialMolblock) {{
        loadModel(initialMolblock, 'mol');
    }}

    function handleFile(file) {{
        if (!file) return;
        const ext = (file.name.split('.').pop() || '').toLowerCase();
        if (!supportedFormats.has(ext)) {{
            showError('ファイル形式に対応していません。pdb / sdf / mol2 / xyz / cube または OpenBabel で変換してください。');
            return;
        }}
        const reader = new FileReader();
        reader.onload = function () {{ loadModel(reader.result, ext); }};
        reader.onerror = function () {{
            showError('ファイル読み込みに失敗しました。');
        }};
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

    document.getElementById('btn-stick').addEventListener('click', function () {{
        viewer.setStyle({{}}, {{ stick: {{}} }}); viewer.render();
    }});
    document.getElementById('btn-bs').addEventListener('click', function () {{
        viewer.setStyle({{}}, DEFAULT_STYLE);
        viewer.render();
    }});
    document.getElementById('btn-sphere').addEventListener('click', function () {{
        viewer.setStyle({{}}, {{ sphere: {{}} }}); viewer.render();
    }});
    document.getElementById('btn-reset').addEventListener('click', function () {{
        if (initialView) {{
            viewer.setView(initialView);
        }} else {{
            viewer.zoomTo();
        }}
        viewer.render();
    }});

    // ---- Atom labels toggle (#3 --label / #4) ---------------------------
    // Default: heavy atoms only. H gets filtered because labelling 9+
    // hydrogens on even a small molecule (ethanol = 6 H) makes the
    // viewer unreadable, and the sphere color already encodes "this
    // small white thing is an H". A shift-click on the button promotes
    // to "show H too" for the rare case where labelling H matters
    // (NMR discussion, exchangeable protons, etc.).
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
                // Explicit font family — 3Dmol bakes labels into a
                // canvas texture so an unspecified font falls back to
                // the host OS default (often a poor sans-serif). Arial
                // gives consistent, crisp glyphs across platforms.
                // fontSize 14 gives a higher-res sprite that doesn't
                // pixelate when zoomed in.
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
        if (labelsOn) {{
            btnLabels.classList.add('toggled');
            btnLabels.textContent = labelsIncludeH
                ? 'ラベル非表示 (H 込み)'
                : 'ラベル非表示';
        }} else {{
            btnLabels.classList.remove('toggled');
            btnLabels.textContent = 'ラベル表示';
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
    const btnLabels = document.getElementById('btn-labels');
    if (btnLabels) {{
        btnLabels.addEventListener('click', function (ev) {{
            // Shift-click promotes to "include H" without an extra
            // button in the controls strip — it's a power-user knob,
            // not the common case.
            if (ev.shiftKey) {{
                toggleLabels(true, !labelsIncludeH);
            }} else {{
                toggleLabels(undefined, false);
            }}
        }});
        btnLabels.title = 'クリックで切替。Shift+クリックで H も表示';
    }}

    // ---- Auto-rotate toggle (#4) ----------------------------------------
    let rotateOn = false;
    function toggleRotate(force) {{
        const want = (force === undefined) ? !rotateOn : Boolean(force);
        if (want === rotateOn) return;
        rotateOn = want;
        if (rotateOn) {{
            viewer.spin('y', 1);
            btnRotate.classList.add('toggled');
            btnRotate.textContent = '回転停止';
        }} else {{
            viewer.spin(false);
            btnRotate.classList.remove('toggled');
            btnRotate.textContent = '自動回転';
        }}
    }}
    const btnRotate = document.getElementById('btn-rotate');
    if (btnRotate) btnRotate.addEventListener('click', function () {{ toggleRotate(); }});

    // ---- SMILES クリップボードコピー (#7) -------------------------------
    const btnCopy = document.getElementById('btn-copy');
    if (btnCopy && initialSmiles) {{
        btnCopy.addEventListener('click', function () {{
            // navigator.clipboard requires a secure context (https). On
            // http://localhost it is still considered secure by every
            // current browser. file:// paths from dev/render_viewer_sample
            // may fail — fall back to the legacy copy path there.
            const text = initialSmiles;
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
    }} else if (btnCopy) {{
        btnCopy.disabled = true;
        btnCopy.title = 'SMILES なし';
    }}

    // ---- PNG ダウンロード (#7) -----------------------------------------
    const btnPng = document.getElementById('btn-save-png');
    if (btnPng) {{
        btnPng.addEventListener('click', function () {{
            try {{
                const dataUrl = viewer.pngURI();
                const a = document.createElement('a');
                a.href = dataUrl;
                a.download = 'molcast-' + (initialMolId || 'viewer') + '.png';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }} catch (e) {{
                showError('画像の保存に失敗しました。');
            }}
        }});
    }}

    // ---- Honor URL query params after the initial model has loaded ----
    // /view/<id>?label=1   -> show labels on load
    // /view/<id>?rotate=1  -> auto-rotate on load
    if (modelLoaded) {{
        try {{
            const params = new URLSearchParams(window.location.search);
            if (params.get('label') === '1') toggleLabels(true);
            if (params.get('rotate') === '1') toggleRotate(true);
        }} catch (e) {{ /* legacy browsers */ }}
    }}
}})();
"""

    # The dropzone is informational (it tells users that drag-and-drop
    # exists and which formats are accepted); leaving it in the a11y
    # tree gives screen-reader users that same affordance.
    dropzone_html = (
        f'<div id="dropzone">{html.escape(drop_hint)}</div>'
        if not molblock
        else ""
    )

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
  {smiles_block}
  <div class="controls">
    <button id="btn-stick" type="button">Stick</button>
    <button id="btn-bs" type="button">Ball &amp; Stick</button>
    <button id="btn-sphere" type="button">Sphere</button>
    <button id="btn-reset" type="button">Reset View</button>
    <button id="btn-labels" type="button">ラベル表示</button>
    <button id="btn-rotate" type="button">自動回転</button>
    <button id="btn-copy" type="button">SMILES をコピー</button>
    <button id="btn-save-png" type="button">画像で保存 (PNG)</button>
  </div>
  <div id="viewer">{dropzone_html}{meta_overlay}</div>
  <div id="error" role="alert"></div>
  <div class="foot">3Dmol.js / RDKit</div>
</div>
<script>{script}</script>
</body>
</html>"""


def render_viewer_2d_html(
    *,
    smiles: str | None,
    svg_text: str | None,
    mol_id: str | None,
    formula: str | None = None,
    mol_weight: float | None = None,
) -> str:
    """Static 2D depiction page for the ``--no-3d`` flag (#3).

    ``svg_text`` is the SVG markup from :func:`rdkit_utils.molblock_to_svg`
    (server-rendered, no 3Dmol.js). The page surfaces SMILES + formula
    + MW alongside the SVG and reuses the same Copy SMILES button.
    """
    smiles_block = ""
    if smiles:
        smiles_block = (
            '<div class="smiles" aria-label="SMILES">'
            f"<strong>SMILES:</strong> {html.escape(smiles)}"
            "</div>"
        )

    meta_block = _render_meta_block(
        smiles=smiles, formula=formula, mol_weight=mol_weight
    )

    # The SVG comes straight from RDKit and is trusted (we built it from
    # our own MolBlock). Embed without escaping — escaping would break
    # the rendering. We trim the XML prolog if RDKit emitted one so the
    # inline SVG doesn't fight the surrounding HTML5 parsing.
    if svg_text:
        cleaned = svg_text
        if cleaned.startswith("<?xml"):
            end = cleaned.find("?>")
            if end != -1:
                cleaned = cleaned[end + 2 :].lstrip()
        svg_html = f'<div class="svg-2d" role="img" aria-label="2D structure">{cleaned}</div>'
    else:
        svg_html = (
            '<div class="svg-2d" role="alert">2D 構造の生成に失敗しました。</div>'
        )

    smiles_js = _embed_js_string(smiles) if smiles else "null"
    mol_id_js = _embed_js_string(mol_id) if mol_id else "null"

    # Minimal JS — only the Copy SMILES button is wired here. Save PNG
    # would require rasterising the SVG, which is browser-fiddly; users
    # can right-click → save the SVG.
    script = f"""
(function () {{
    const initialSmiles = {smiles_js};
    const initialMolId = {mol_id_js};
    const btnCopy = document.getElementById('btn-copy');
    if (!btnCopy || !initialSmiles) {{
        if (btnCopy) {{ btnCopy.disabled = true; btnCopy.title = 'SMILES なし'; }}
        return;
    }}
    btnCopy.addEventListener('click', function () {{
        const text = initialSmiles;
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
  {smiles_block}
  {meta_block}
  <div class="controls">
    <button id="btn-copy" type="button">SMILES をコピー</button>
  </div>
  {svg_html}
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
