"""Tests for §6.1 (RDKit pipeline)."""
from __future__ import annotations

import pytest

from app.rdkit_utils import (
    EMBED_SEEDS,
    MoleculeGenerationError,
    generate_3d_molblock,
)


def test_ccO_generates_molblock():
    molblock = generate_3d_molblock("CCO")
    assert "M  END" in molblock
    # LF-only, per §6.1 step 6.
    assert "\r" not in molblock
    # Ethanol after AddHs has 9 atoms.
    lines = molblock.splitlines()
    counts_line = lines[3]
    n_atoms = int(counts_line[:3])
    assert n_atoms == 9


def test_invalid_smiles_raises_user_error():
    with pytest.raises(MoleculeGenerationError) as excinfo:
        generate_3d_molblock("not_a_smiles")
    assert "SMILES" in str(excinfo.value)


def test_empty_string_raises_user_error():
    with pytest.raises(MoleculeGenerationError):
        generate_3d_molblock("")
    with pytest.raises(MoleculeGenerationError):
        generate_3d_molblock("   ")


def test_max_atoms_blocks_oversize_molecule_using_post_addhs_count():
    # Octane (CCCCCCCC): 8 heavy atoms, 26 atoms after AddHs.
    # With max_atoms=10 the gate must fire even though the heavy-atom
    # count is BELOW the threshold; this pins §6.1 step 3 ("H 付加後の
    # 原子数が max_atoms を超える") and would catch a future refactor
    # that moved the gate to before AddHs.
    with pytest.raises(MoleculeGenerationError) as excinfo:
        generate_3d_molblock("CCCCCCCC", max_atoms=10)
    assert "200" not in str(excinfo.value)
    assert "上限 10" in str(excinfo.value)


def test_max_atoms_allows_when_post_addhs_count_is_under_limit():
    # Methane (C): 1 heavy + 4 H = 5 atoms total. With max_atoms=5
    # the gate must NOT fire (post-AddHs == threshold, brief uses `>`).
    molblock = generate_3d_molblock("C", max_atoms=5)
    assert "M  END" in molblock


def test_default_max_atoms_value():
    # Spec says default = 200 (§10 MAX_ATOMS). Verify the signature default.
    from inspect import signature

    sig = signature(generate_3d_molblock)
    assert sig.parameters["max_atoms"].default == 200


def test_uff_branch_taken_when_mmff_returns_none(monkeypatch):
    """§12.1 demands 'MMFF 不可な分子で UFF が呼ばれること'. Force
    MMFFGetMoleculeProperties to return None and assert that UFF is the
    one optimiser actually invoked — this catches a regression that
    inverted the None-check (which the [SnH4] heuristic would not)."""
    from app import rdkit_utils

    calls = {"mmff": 0, "uff": 0}

    def fake_mmff_props(_mol):
        return None

    def fake_uff(_mol, **_kwargs):
        calls["uff"] += 1
        return 0

    def fake_mmff_opt(_mol, **_kwargs):
        calls["mmff"] += 1
        return 0

    monkeypatch.setattr(
        rdkit_utils.AllChem, "MMFFGetMoleculeProperties", fake_mmff_props
    )
    monkeypatch.setattr(
        rdkit_utils.AllChem, "UFFOptimizeMolecule", fake_uff
    )
    monkeypatch.setattr(
        rdkit_utils.AllChem, "MMFFOptimizeMolecule", fake_mmff_opt
    )

    molblock = generate_3d_molblock("CCO")
    assert "M  END" in molblock
    assert calls["uff"] == 1
    assert calls["mmff"] == 0


def test_mmff_branch_taken_when_props_available(monkeypatch):
    """Mirror of the UFF-branch test: when MMFFGetMoleculeProperties
    returns a truthy value, MMFF must be invoked and UFF must not."""
    from app import rdkit_utils

    calls = {"mmff": 0, "uff": 0}

    sentinel_props = object()  # any truthy non-None value

    def fake_mmff_props(_mol):
        return sentinel_props

    def fake_uff(_mol, **_kwargs):
        calls["uff"] += 1
        return 0

    def fake_mmff_opt(_mol, **_kwargs):
        calls["mmff"] += 1
        return 0

    monkeypatch.setattr(
        rdkit_utils.AllChem, "MMFFGetMoleculeProperties", fake_mmff_props
    )
    monkeypatch.setattr(
        rdkit_utils.AllChem, "UFFOptimizeMolecule", fake_uff
    )
    monkeypatch.setattr(
        rdkit_utils.AllChem, "MMFFOptimizeMolecule", fake_mmff_opt
    )

    molblock = generate_3d_molblock("CCO")
    assert "M  END" in molblock
    assert calls["mmff"] == 1
    assert calls["uff"] == 0


def test_embed_seeds_are_three():
    # §6.1 fixes the retry budget at 3. Guard against a future edit that
    # silently bumps the count without updating the parameter list.
    assert len(EMBED_SEEDS) == 3
