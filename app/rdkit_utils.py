"""RDKit pipeline: SMILES -> 3D MolBlock (see §6.1 of the design brief).

The retry policy is fixed at three attempts because each attempt uses a
distinct ETKDGv3 parameter set; a four-th attempt would have no defined
parameters. The retry count is intentionally NOT a configurable
environment variable (see §10).
"""
from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import AllChem


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
