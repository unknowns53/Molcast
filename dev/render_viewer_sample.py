"""Render the viewer HTML to a local file without spinning up uvicorn.

Use case: iterate on `app/templates.py` (CSS, 3Dmol.js style, JS UI) and
see the result in a browser in seconds, without a Cloud Run deploy or
even a local FastAPI server. Pair with `app/templates.py` open in the
editor — each `python dev/render_viewer_sample.py --open` is one
iteration.

Usage:
    .venv/Scripts/python.exe dev/render_viewer_sample.py
    .venv/Scripts/python.exe dev/render_viewer_sample.py --smiles "C/C=C/C"
    # Multi-frame trajectory: ``;``-separated SMILES, same as /mol
    .venv/Scripts/python.exe dev/render_viewer_sample.py \
        --smiles "CCO ; CC(C)O ; CC(C)(C)O" --open
    .venv/Scripts/python.exe dev/render_viewer_sample.py --bare --open
    .venv/Scripts/python.exe dev/render_viewer_sample.py --mode 2d --open

The default output path is `dev/_renders/sample.html` (gitignored).
"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

# Allow `python dev/render_viewer_sample.py` from the repo root without
# installing the project as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import templates
from app.rdkit_utils import (
    MoleculeGenerationError,
    generate_3d_molblock,
    molblock_to_formula_and_weight,
    molblock_to_svg,
)


def _build_args() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--smiles",
        default="CCO",
        help=(
            'SMILES to render (default: "CCO", ethanol). Use ``;`` to '
            "render a multi-frame trajectory (the viewer ships a "
            "navigation strip). Ignored with --bare."
        ),
    )
    p.add_argument(
        "--bare",
        action="store_true",
        help=(
            "Render the empty viewer (drop-zone for coordinate files), the "
            "same one served at /view/ in prod. Skips RDKit entirely."
        ),
    )
    p.add_argument(
        "--mol-id",
        default="dev-render",
        help=(
            "Synthetic mol_id stamped into the page (default: dev-render). "
            "Has no effect on the rendered viewer, only on data attributes "
            "used by templates."
        ),
    )
    p.add_argument(
        "--out",
        type=Path,
        default=_REPO_ROOT / "dev" / "_renders" / "sample.html",
        help=(
            "Output HTML file. Default: dev/_renders/sample.html (gitignored). "
            "Parent dir is created if missing."
        ),
    )
    p.add_argument(
        "--open",
        action="store_true",
        help="After writing, open the file in the default web browser.",
    )
    p.add_argument(
        "--mode",
        choices=("3d", "2d"),
        default="3d",
        help=(
            "Render the 3D viewer (default) or the 2D SVG page used by "
            "the /mol --no-3d flag (#3). Ignored with --bare."
        ),
    )
    p.add_argument(
        "--no-meta",
        action="store_true",
        help=(
            "Skip the formula / MW meta pill block. Default behaviour "
            "is to compute them from the MolBlock and pass to the template."
        ),
    )
    return p


def _split_segments(text: str) -> list[str]:
    """Mirror the server-side ``app.main._split_segments`` semantics for
    SMILES-only inputs: split on ``;``, strip whitespace, drop empties.
    """
    return [s for s in (p.strip() for p in text.split(";")) if s]


def _build_frame(
    smiles: str, *, include_meta: bool, render_svg: bool
) -> dict | None:
    """Run one SMILES through RDKit and (optionally) RDKit Draw, returning
    the frame dict the template expects. ``None`` means "skip this one
    and print an error" — preserves the order of remaining frames.
    """
    try:
        molblock = generate_3d_molblock(smiles, max_atoms=200)
    except MoleculeGenerationError as exc:
        print(f"  [skip] {smiles!r}: {exc}", file=sys.stderr)
        return None
    formula = None
    mol_weight = None
    if include_meta:
        formula, mol_weight = molblock_to_formula_and_weight(molblock)
    svg_text = molblock_to_svg(molblock) if render_svg else None
    return {
        "kind": "smiles",
        "input": smiles,
        "smiles": smiles,
        "molblock": molblock,
        "formula": formula,
        "mol_weight": mol_weight,
        "svg_text": svg_text,
        "error": None,
    }


def main() -> int:
    args = _build_args().parse_args()

    if args.bare:
        print("rendering bare viewer (drop-zone only)")
        html = templates.render_viewer_html(frames=[])
    else:
        segments = _split_segments(args.smiles)
        if not segments:
            print("ERROR: --smiles is empty", file=sys.stderr)
            return 2
        include_meta = not args.no_meta
        render_svg = args.mode == "2d"
        print(
            f"rendering {len(segments)} frame(s) "
            f"(mode={args.mode}, meta={'on' if include_meta else 'off'})"
        )
        frames: list[dict] = []
        for smi in segments:
            frame = _build_frame(
                smi, include_meta=include_meta, render_svg=render_svg
            )
            if frame is not None:
                frames.append(frame)
                tag = (
                    f"formula={frame['formula']} MW={frame['mol_weight']:.2f}"
                    if frame["formula"] else "(no meta)"
                )
                print(f"  [{len(frames)}] {smi!r} -> {tag}")
        if not frames:
            print("ERROR: no frames rendered (all SMILES failed)", file=sys.stderr)
            return 2
        if args.mode == "2d":
            html = templates.render_viewer_2d_html(
                frames=frames, mol_id=args.mol_id
            )
        else:
            html = templates.render_viewer_html(
                frames=frames, mol_id=args.mol_id
            )

    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {len(html)} chars to {out}")

    if args.open:
        # webbrowser.open of a local Path needs a file:// URL on Windows.
        url = out.absolute().as_uri()
        webbrowser.open(url)
        print(f"opened {url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
