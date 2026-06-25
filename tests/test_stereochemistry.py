"""Regression tests for §12.3 (E/Z and R/S stereochemistry preservation)."""
from __future__ import annotations

from rdkit import Chem

from app.rdkit_utils import generate_3d_molblock


def _smiles_from_molblock(mb: str) -> str:
    mol = Chem.MolFromMolBlock(mb, removeHs=False)
    return Chem.MolToSmiles(mol)


def test_ez_isomers_differ():
    mb_e = generate_3d_molblock("C/C=C/C")
    mb_z = generate_3d_molblock("C/C=C\\C")
    # The coordinates must differ (rigid geometry shifts substituent
    # positions). Comparing the entire MolBlock string is sensitive
    # enough.
    assert mb_e != mb_z


def test_ez_stereodescriptors_round_trip():
    mb_e = generate_3d_molblock("C/C=C/C")
    mb_z = generate_3d_molblock("C/C=C\\C")
    s_e = _smiles_from_molblock(mb_e)
    s_z = _smiles_from_molblock(mb_z)
    # Round-trip SMILES must encode the original double-bond geometry.
    assert "/" in s_e or "\\" in s_e
    assert "/" in s_z or "\\" in s_z
    assert s_e != s_z


def test_rs_isomers_differ():
    mb_l = generate_3d_molblock("C[C@H](N)C(=O)O")
    mb_d = generate_3d_molblock("C[C@@H](N)C(=O)O")
    assert mb_l != mb_d


def test_rs_stereodescriptors_round_trip():
    mb_l = generate_3d_molblock("C[C@H](N)C(=O)O")
    mb_d = generate_3d_molblock("C[C@@H](N)C(=O)O")
    s_l = _smiles_from_molblock(mb_l)
    s_d = _smiles_from_molblock(mb_d)
    # Round-trip should preserve the @/@@ marker (canonical form may
    # invert representation, so just compare for difference).
    assert "@" in s_l
    assert "@" in s_d
    assert s_l != s_d
