"""Tests for §6.3 / §9.7 (OPSIN name -> SMILES resolution).

All external integrations are mocked: ``subprocess.run`` for the local
JVM call and ``httpx.get`` for the EBI Web call. JRE is not required to
run these tests; CI does NOT need Temurin installed.

Alias-JSON round-trip and duplicate-key checks run RDKit but no
subprocess and no network.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import httpx
import pytest

from app import opsin_utils
from app.rdkit_utils import MoleculeGenerationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_alias_cache():
    """The alias dict is cached at module scope across tests; the cache
    is correct in production but invalidating between tests guards
    against an accidental future change loading test-time JSON.
    """
    opsin_utils._ALIASES = None
    yield
    opsin_utils._ALIASES = None


def _fake_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["java"], returncode=returncode, stdout=stdout, stderr=""
    )


def _httpx_resp(status: int, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=text,
        request=httpx.Request("GET", "https://opsin.example/x"),
    )


# ---------------------------------------------------------------------------
# Alias hit path
# ---------------------------------------------------------------------------
def test_alias_hit_returns_smiles_without_external_call():
    """``DMSO`` is in the alias JSON; no JVM, no HTTP."""
    with mock.patch.object(opsin_utils.subprocess, "run") as mrun, mock.patch.object(
        opsin_utils.httpx, "get"
    ) as mget:
        smiles = opsin_utils.iupac_to_smiles("DMSO", backend="local")
    assert smiles == "CS(C)=O"
    assert mrun.call_count == 0
    assert mget.call_count == 0


def test_alias_hit_is_case_insensitive_and_strips_whitespace():
    with mock.patch.object(opsin_utils.subprocess, "run") as mrun, mock.patch.object(
        opsin_utils.httpx, "get"
    ) as mget:
        smiles = opsin_utils.iupac_to_smiles("  Ethanol  ", backend="web")
    assert smiles == "CCO"
    assert mrun.call_count == 0
    assert mget.call_count == 0


def test_alias_hit_handles_kana_entry():
    """Kana names are stored verbatim; ``_normalize`` lowercases ASCII
    only and is a no-op for kana."""
    with mock.patch.object(opsin_utils.subprocess, "run") as mrun, mock.patch.object(
        opsin_utils.httpx, "get"
    ) as mget:
        smiles = opsin_utils.iupac_to_smiles("メタノール", backend="local")
    assert smiles == "CO"
    assert mrun.call_count == 0
    assert mget.call_count == 0


# ---------------------------------------------------------------------------
# Web backend
# ---------------------------------------------------------------------------
def test_backend_web_calls_ebi_endpoint_for_unknown_name():
    """Name not in alias mapping -> Web call (no JVM in web mode)."""
    with mock.patch.object(
        opsin_utils.httpx, "get", return_value=_httpx_resp(200, "C1=CC=CC=C1\n")
    ) as mget, mock.patch.object(opsin_utils.subprocess, "run") as mrun:
        smiles = opsin_utils.iupac_to_smiles(
            "benzene-d6-fake-name-not-in-alias", backend="web"
        )
    assert smiles == "C1=CC=CC=C1"
    assert mget.call_count == 1
    assert mrun.call_count == 0
    # URL-encode the name and use the .smi suffix.
    called_url = mget.call_args.args[0]
    assert called_url.endswith(".smi")
    assert "benzene-d6-fake-name-not-in-alias" in called_url


def test_backend_web_empty_body_treated_as_failure():
    with mock.patch.object(
        opsin_utils.httpx, "get", return_value=_httpx_resp(200, "   \n")
    ):
        with pytest.raises(MoleculeGenerationError) as excinfo:
            opsin_utils.iupac_to_smiles("uninterpretable-name", backend="web")
    assert "OPSIN" in str(excinfo.value)


def test_backend_web_404_raises():
    with mock.patch.object(
        opsin_utils.httpx, "get", return_value=_httpx_resp(404, "")
    ):
        with pytest.raises(MoleculeGenerationError):
            opsin_utils.iupac_to_smiles("totally-bogus-fake-name", backend="web")


def test_backend_web_network_error_raises():
    err = httpx.ConnectError("nope", request=httpx.Request("GET", "https://x"))
    with mock.patch.object(opsin_utils.httpx, "get", side_effect=err):
        with pytest.raises(MoleculeGenerationError):
            opsin_utils.iupac_to_smiles("totally-bogus-fake-name-2", backend="web")


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------
def test_backend_local_only_uses_subprocess_and_returns_smiles(tmp_path):
    """A non-alias name with ``local_only`` exercises the subprocess
    path. ``OPSIN_JAR_PATH`` env points at a fake file so we skip the
    py2opsin glob entirely.
    """
    fake_jar = tmp_path / "opsin.jar"
    fake_jar.write_bytes(b"")
    with mock.patch.object(
        opsin_utils.subprocess, "run", return_value=_fake_completed("c1ccccc1\n")
    ) as mrun, mock.patch.object(opsin_utils.httpx, "get") as mget:
        smiles = opsin_utils.iupac_to_smiles(
            "hexafluorobenzene-fake",
            backend="local_only",
            jar_path=str(fake_jar),
        )
    assert smiles == "c1ccccc1"
    assert mrun.call_count == 1
    assert mget.call_count == 0
    # OPSIN flag is the all-in-one ``-osmi``, not ``-o smi``.
    assert "-osmi" in mrun.call_args.args[0]


def test_backend_local_only_raises_when_subprocess_times_out(tmp_path):
    fake_jar = tmp_path / "opsin.jar"
    fake_jar.write_bytes(b"")
    with mock.patch.object(
        opsin_utils.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd=["java"], timeout=10),
    ), mock.patch.object(opsin_utils.httpx, "get") as mget:
        with pytest.raises(MoleculeGenerationError):
            opsin_utils.iupac_to_smiles(
                "long-name", backend="local_only", jar_path=str(fake_jar)
            )
    assert mget.call_count == 0


def test_backend_local_only_raises_when_java_not_on_path(tmp_path):
    fake_jar = tmp_path / "opsin.jar"
    fake_jar.write_bytes(b"")
    with mock.patch.object(
        opsin_utils.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(opsin_utils.httpx, "get") as mget:
        with pytest.raises(MoleculeGenerationError):
            opsin_utils.iupac_to_smiles(
                "anything", backend="local_only", jar_path=str(fake_jar)
            )
    assert mget.call_count == 0


def test_backend_local_only_raises_when_jar_path_does_not_exist():
    """No JAR resolvable -> no local call attempted -> raise."""
    with mock.patch.object(opsin_utils.httpx, "get") as mget, mock.patch.object(
        opsin_utils, "_resolve_jar_path", return_value=None
    ):
        with pytest.raises(MoleculeGenerationError):
            opsin_utils.iupac_to_smiles(
                "anything", backend="local_only", jar_path="/nope.jar"
            )
    assert mget.call_count == 0


def test_backend_local_falls_back_to_web_on_subprocess_timeout(tmp_path):
    """``local`` (default) -> py2opsin times out -> EBI Web rescues."""
    fake_jar = tmp_path / "opsin.jar"
    fake_jar.write_bytes(b"")
    with mock.patch.object(
        opsin_utils.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd=["java"], timeout=10),
    ) as mrun, mock.patch.object(
        opsin_utils.httpx,
        "get",
        return_value=_httpx_resp(200, "CCCCO\n"),
    ) as mget:
        smiles = opsin_utils.iupac_to_smiles(
            "1-butanol-fake-not-in-alias",
            backend="local",
            jar_path=str(fake_jar),
        )
    assert smiles == "CCCCO"
    assert mrun.call_count == 1
    assert mget.call_count == 1


def test_backend_local_falls_back_to_web_when_jar_unresolvable():
    with mock.patch.object(
        opsin_utils, "_resolve_jar_path", return_value=None
    ), mock.patch.object(
        opsin_utils.httpx, "get", return_value=_httpx_resp(200, "CCO\n")
    ) as mget, mock.patch.object(opsin_utils.subprocess, "run") as mrun:
        smiles = opsin_utils.iupac_to_smiles(
            "ethanol-fake-not-in-alias", backend="local"
        )
    assert smiles == "CCO"
    assert mrun.call_count == 0
    assert mget.call_count == 1


def test_backend_local_both_fail_raises(tmp_path):
    fake_jar = tmp_path / "opsin.jar"
    fake_jar.write_bytes(b"")
    with mock.patch.object(
        opsin_utils.subprocess,
        "run",
        return_value=_fake_completed("", returncode=0),
    ), mock.patch.object(
        opsin_utils.httpx, "get", return_value=_httpx_resp(404, "")
    ):
        with pytest.raises(MoleculeGenerationError):
            opsin_utils.iupac_to_smiles(
                "unparseable", backend="local", jar_path=str(fake_jar)
            )


# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bogus", ["", "   ", "\x00\x00", "ethanol\nDMSO", "x\rEtOH"])
def test_invalid_input_raises_without_external_call(bogus):
    """Empty / null-byte / newline-bearing inputs are user errors and
    must not touch subprocess or httpx.
    """
    with mock.patch.object(opsin_utils.subprocess, "run") as mrun, mock.patch.object(
        opsin_utils.httpx, "get"
    ) as mget:
        with pytest.raises(MoleculeGenerationError):
            opsin_utils.iupac_to_smiles(bogus, backend="local")
    assert mrun.call_count == 0
    assert mget.call_count == 0


# ---------------------------------------------------------------------------
# Alias JSON well-formedness (CI guard, not runtime cost)
# ---------------------------------------------------------------------------
_ALIAS_JSON_PATH = Path(__file__).resolve().parents[1] / "app" / "opsin_aliases.json"


def _load_alias_data():
    return json.loads(_ALIAS_JSON_PATH.read_text(encoding="utf-8"))


def test_all_alias_smiles_parse_with_rdkit():
    """RDKit must accept every SMILES in the alias JSON. ``verified``
    entries get strict round-trip equality; unverified ones only need
    to parse (their canonical SMILES is yet to be cross-checked
    against PubChem, see ``_meta.validation_recommendation``).
    """
    from rdkit import Chem

    data = _load_alias_data()
    bad: list[str] = []
    for compound in data["compounds"]:
        smi = compound["smiles"]
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            bad.append(smi)
            continue
        if compound.get("verified", False):
            roundtrip = Chem.MolToSmiles(Chem.MolFromSmiles(Chem.MolToSmiles(mol)))
            if roundtrip != Chem.MolToSmiles(mol):
                bad.append(f"{smi} (round-trip drift)")
    assert not bad, f"invalid SMILES in alias JSON: {bad}"


def test_alias_names_are_unique_after_normalization():
    """The flattened ``{normalized_name: smiles}`` dict would silently
    overwrite duplicates; surface them here instead.
    """
    data = _load_alias_data()
    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str, str]] = []
    for compound in data["compounds"]:
        for raw in compound["names"]:
            key = opsin_utils._normalize(raw)
            if key in seen and seen[key] != compound["smiles"]:
                duplicates.append((key, seen[key], compound["smiles"]))
            seen[key] = compound["smiles"]
    assert not duplicates, f"duplicate normalized names: {duplicates}"
