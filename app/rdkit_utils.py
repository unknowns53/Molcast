"""RDKit pipeline: SMILES -> 3D MolBlock (see §6.1 of the design brief).

The retry policy is fixed at three attempts because each attempt uses a
distinct ETKDGv3 parameter set; a four-th attempt would have no defined
parameters. The retry count is intentionally NOT a configurable
environment variable (see §10).
"""
from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import AllChem, Descriptors
from rdkit.Chem.Draw import rdMolDraw2D


EMBED_SEEDS: tuple[int, ...] = (0xF00D, 0xBEEF, 0x1234)


class MoleculeGenerationError(Exception):
    """Raised when an input cannot be turned into a 3D MolBlock.

    The message attribute is end-user-friendly Japanese text and may be
    surfaced directly via Slack ``response_url`` or HTML.
    """


def _build_embed_params(attempt: int, seed: int):
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if attempt == 0:
        params.useRandomCoords = False
    elif attempt == 1:
        params.useRandomCoords = True
    else:
        params.useRandomCoords = True
        params.maxAttempts = 200
    return params


def generate_3d_molblock(smiles: str, max_atoms: int = 200) -> str:
    """Return an MMFF/UFF-optimized 3D MolBlock for ``smiles``.

    Steps follow §6.1 of the design brief:
      1. ``MolFromSmiles`` (None -> MoleculeGenerationError).
      2. ``AddHs``.
      3. Atom-count gate against ``max_atoms`` (after AddHs).
      4. Three-attempt ETKDGv3 embed with the seeds in ``EMBED_SEEDS``.
      5. MMFF if available, else UFF (both with ``maxIters=200``;
         unconverged returns are tolerated).
      6. Return MolBlock with LF-normalised line endings.
    """
    if not isinstance(smiles, str) or not smiles.strip():
        raise MoleculeGenerationError(
            "SMILES の解釈に失敗しました。表記をご確認ください。"
        )

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise MoleculeGenerationError(
            "SMILES の解釈に失敗しました。表記をご確認ください。"
        )

    mol = Chem.AddHs(mol)

    # H 付加後の原子数で判定する (§6.1 step 3)。
    if mol.GetNumAtoms() > max_atoms:
        raise MoleculeGenerationError(
            f"分子が大きすぎます (上限 {max_atoms} 原子)。"
            "座標ファイルを直接ビューアに D&D してください。"
        )

    embed_status = -1
    for attempt, seed in enumerate(EMBED_SEEDS):
        params = _build_embed_params(attempt, seed)
        embed_status = AllChem.EmbedMolecule(mol, params)
        if embed_status != -1:
            break

    if embed_status == -1:
        raise MoleculeGenerationError(
            "3D 構造の生成に失敗しました。立体的に困難な構造の可能性があります。"
        )

    props = AllChem.MMFFGetMoleculeProperties(mol)
    if props is None:
        # MMFF cannot describe this molecule (e.g. unsupported atom type);
        # fall back to UFF. Unconverged (return code 1) is tolerated.
        AllChem.UFFOptimizeMolecule(mol, maxIters=200)
    else:
        AllChem.MMFFOptimizeMolecule(mol, maxIters=200)

    molblock = Chem.MolToMolBlock(mol)
    # Force LF line endings for downstream test stability (§6.1 step 6).
    return molblock.replace("\r\n", "\n").replace("\r", "\n")


def count_atoms_with_hs(smiles: str) -> int | None:
    """Helper for log/debug use: atom count after AddHs, or ``None`` if
    the SMILES does not parse."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    return Chem.AddHs(mol).GetNumAtoms()


# ---------------------------------------------------------------------------
# Viewer metadata helpers (#6, #3 --no-3d)
# ---------------------------------------------------------------------------
def molblock_to_formula_and_weight(
    molblock: str,
) -> tuple[str | None, float | None]:
    """Return ``(formula, mol_weight)`` for a stored MolBlock.

    Both fields are ``None`` on parse failure. The MolBlock is the
    canonical structure (it carries explicit hydrogens from our embed
    pipeline) so ``MolWt`` uses the right H count without re-AddHs.
    Defensive: any RDKit exception falls through to ``(None, None)`` —
    the viewer is still useful without the meta pills.
    """
    if not molblock:
        return (None, None)
    try:
        mol = Chem.MolFromMolBlock(molblock, removeHs=False)
        if mol is None:
            return (None, None)
        formula = Chem.rdMolDescriptors.CalcMolFormula(mol)
        weight = float(Descriptors.MolWt(mol))
        return (formula, weight)
    except Exception:
        return (None, None)


def molblock_to_svg(
    molblock: str, *, width: int = 600, height: int = 480
) -> str | None:
    """Render a 2D depiction from the stored MolBlock as SVG text.

    Used by the ``--no-3d`` flag (#3). Returns ``None`` on parse
    failure. The 2D coordinates are computed fresh via
    ``Chem.AllChem.Compute2DCoords`` because our stored MolBlock holds
    3D coordinates from the embed step, and ``MolDraw2DSVG`` would
    otherwise project them into the page plane (cluttered overlap).

    Hydrogens are stripped before depiction — a flat 2D drawing with
    every H drawn is unreadable for any molecule larger than a few
    atoms.
    """
    if not molblock:
        return None
    try:
        mol = Chem.MolFromMolBlock(molblock, removeHs=False)
        if mol is None:
            return None
        mol = Chem.RemoveHs(mol)
        AllChem.Compute2DCoords(mol)
        drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
        drawer.DrawMolecule(mol)
        drawer.FinishDrawing()
        return drawer.GetDrawingText()
    except Exception:
        return None
