r"""HTML rendering for the viewer (see §7 of the design brief).

A single ``render_viewer_html`` covers three call shapes:

  1. ``/view/{id}``  -> smiles + molblock + mol_id are all set; the viewer
                        loads the MolBlock on page load.
  2. ``/view/`` or ``/``
                     -> molblock and mol_id are both ``None``; the page
                        shows a drag-and-drop drop zone for coordinate
                        files (pdb / sdf / mol2 / xyz / cube).
  3. Edge: smiles set but molblock missing -> treated as #2 with the
     SMILES line displayed as a hint.

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
.controls { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.controls button {
    min-height: 44px; min-width: 44px; padding: 8px 14px;
    border: 1px solid #bbb; border-radius: 4px; background: #fff;
    font-size: 0.95rem; cursor: pointer;
}
.controls button:hover { background: #f0f0f0; }
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
.foot { margin-top: 8px; font-size: 0.8rem; color: #888; }
"""


def render_viewer_html(
    smiles: str | None,
    molblock: str | None,
    mol_id: str | None,
    *,
    supported_formats: tuple[str, ...] = ("pdb", "sdf", "mol2", "xyz", "cube"),
) -> str:
    smiles_block = ""
    if smiles:
        smiles_block = (
            '<div class="smiles" aria-label="SMILES">'
            f"<strong>SMILES:</strong> {html.escape(smiles)}"
            "</div>"
        )

    molblock_js = _embed_js_string(molblock) if molblock else "null"
    formats_js = json.dumps(list(supported_formats))

    drop_hint = (
        "ファイルをここにドロップ "
        f"({', '.join(supported_formats)})"
    )

    script = f"""
(function () {{
    const initialMolblock = {molblock_js};
    const supportedFormats = new Set({formats_js});
    const errorBox = document.getElementById('error');
    const dropzone = document.getElementById('dropzone');
    const viewerEl = document.getElementById('viewer');

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

    function loadModel(text, fmt) {{
        try {{
            viewer.clear();
            viewer.addModel(text, fmt);
            viewer.setStyle({{}}, {{ stick: {{ radius: 0.15 }}, sphere: {{ scale: 0.25 }} }});
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
        viewer.setStyle({{}}, {{ stick: {{ radius: 0.15 }}, sphere: {{ scale: 0.25 }} }});
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
  </div>
  <div id="viewer">{dropzone_html}</div>
  <div id="error" role="alert"></div>
  <div class="foot">3Dmol.js / RDKit</div>
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
