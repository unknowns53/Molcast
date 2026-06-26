r"""IUPAC / trivial name -> SMILES (see §6.3, §9.7 of the design brief).

Three-stage resolution:

  1. ``opsin_aliases.json`` — static mapping for trivial names that
     OPSIN cannot parse (DMSO, THF, etc.) and for kana entries. Fast
     path, no JVM, no network.
  2. Local OPSIN (subprocess + JRE). The CLI JAR ships inside the
     ``py2opsin`` wheel; we locate it via ``_resolve_jar_path`` and
     invoke ``java -jar ... -osmi <tmpfile>`` directly so we can pass
     ``subprocess.run(timeout=...)``. py2opsin's own Python API does
     not expose a per-call timeout, and its temp-file name is fixed
     (``py2opsin_temp_input.txt``) which would collide under parallel
     Cloud Run requests; ``tempfile.NamedTemporaryFile`` avoids both.
  3. EBI OPSIN Web (``https://opsin.ch.cam.ac.uk/opsin/<name>.smi``).
     Single attempt, 5 s timeout, no retry — repeated failure is
     usually the input being un-parseable, not a transient outage.

Backend modes (``OPSIN_BACKEND`` env, §10):

  * ``local``      — alias -> local OPSIN -> EBI Web. Default.
  * ``local_only`` — alias -> local OPSIN. No web fallback; for CI
                     where JRE absence should fail loudly.
  * ``web``        — alias -> EBI Web. Skips the local path entirely
                     so a JRE-free dev host can still run the name
                     route. ``py2opsin`` is NOT imported in this mode.

All-path failure raises :class:`MoleculeGenerationError` with a
user-facing Japanese message that nudges the user toward
``/mol <SMILES>``.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Literal

import httpx

from .config import get_settings
from .rdkit_utils import MoleculeGenerationError


logger = logging.getLogger("molcast.opsin")

OpsinBackend = Literal["local", "local_only", "web"]


_ALIASES_PATH = Path(__file__).parent / "opsin_aliases.json"
_ALIASES: dict[str, str] | None = None


_USER_AGENT = "molcast/0.1"
# OPSIN がパースできない場合のユーザ向けメッセージ。具体例を 3 行添えて
# 「次に何を打てばいいか」を即座に示す (#2)。Slack ではバッククォートで
# コード扱いになるので、コピー&ペーストでそのまま動く。
_FALLBACK_ERROR_MESSAGE = (
    "OPSIN は体系名のみ対応です。慣用名・商品名は解釈できません。\n"
    "SMILES を直接入力するか、登録済みエイリアスをお使いください。例:\n"
    "  • `/mol CCO` (エタノール)\n"
    "  • `/mol c1ccccc1` (ベンゼン)\n"
    "  • `/mol name: DMSO` (エイリアス経由)"
)


def _normalize(name: str) -> str:
    """Match the ``_meta.lookup`` rule in ``opsin_aliases.json``: lower-case
    the ASCII portion, strip surrounding whitespace; Japanese kana pass
    through unchanged because ``str.lower()`` is a no-op for them.
    """
    return name.strip().lower()


def _load_aliases() -> dict[str, str]:
    """Read ``opsin_aliases.json`` once and flatten ``compounds[].names`` into
    a ``{normalized_name: smiles}`` dict. Cached at module scope. Malformed
    JSON is a deployment defect, not a user error, so we let the
    ``KeyError`` / ``json.JSONDecodeError`` propagate.
    """
    global _ALIASES
    if _ALIASES is not None:
        return _ALIASES

    data = json.loads(_ALIASES_PATH.read_text(encoding="utf-8"))
    table: dict[str, str] = {}
    for compound in data.get("compounds", []):
        smiles = compound["smiles"]
        for raw_name in compound.get("names", []):
            table[_normalize(raw_name)] = smiles
    _ALIASES = table
    return _ALIASES


def _try_alias(name: str) -> str | None:
    return _load_aliases().get(_normalize(name))


def _resolve_jar_path(explicit_path: str | None) -> str | None:
    """Locate the OPSIN CLI JAR.

    Order:
      1. ``explicit_path`` (env ``OPSIN_JAR_PATH``) if it points at a
         real file — lets Docker / CI pin a specific JAR.
      2. The JAR bundled inside ``py2opsin``'s wheel (glob to survive
         version bumps; pinned to ``opsin-cli-2.9.0-...`` at time of
         writing, but py2opsin will rename it on upgrades).

    Returns ``None`` if both fail; the caller treats that as "local
    backend unavailable" and falls back.
    """
    if explicit_path:
        p = Path(explicit_path)
        if p.is_file():
            return str(p)

    try:
        import py2opsin  # noqa: F401  (loaded only for __file__)
    except Exception as exc:
        logger.info(
            "opsin_jar_resolution_failed",
            extra={"error_kind": type(exc).__name__, "backend": "local"},
        )
        return None

    pkg_dir = Path(py2opsin.__file__).parent
    candidates = sorted(pkg_dir.glob("opsin-cli-*-jar-with-dependencies.jar"))
    if candidates:
        return str(candidates[0])

    logger.info(
        "opsin_jar_resolution_failed",
        extra={"error_kind": "NoBundledJar", "backend": "local"},
    )
    return None


def _call_local(name: str, *, jar_path: str, timeout: float) -> str | None:
    """Invoke OPSIN CLI in a subprocess. Returns the SMILES on success,
    ``None`` on any failure (timeout, non-zero exit, empty stdout,
    missing ``java`` on PATH, missing JAR file).

    OPSIN reads its input from a file path passed as the last argument,
    one chemical name per line. ``tempfile.NamedTemporaryFile`` gives a
    unique path per call so concurrent requests do not stomp on each
    other (py2opsin's hard-coded ``py2opsin_temp_input.txt`` does).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(name)
        tmp_path = fh.name

    try:
        result = subprocess.run(
            ["java", "-jar", jar_path, "-osmi", tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.info(
            "opsin_local_timeout",
            extra={"backend": "local", "error_kind": "TimeoutExpired"},
        )
        return None
    except FileNotFoundError:
        logger.info(
            "opsin_local_no_java",
            extra={"backend": "local", "error_kind": "FileNotFoundError"},
        )
        return None
    except OSError as exc:
        logger.info(
            "opsin_local_os_error",
            extra={"backend": "local", "error_kind": type(exc).__name__},
        )
        return None
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    if result.returncode != 0:
        return None

    smiles = result.stdout.strip()
    return smiles or None


def _call_web(name: str, *, web_url: str, timeout: float) -> str | None:
    """``GET {web_url}{quoted_name}.smi`` — return the SMILES on a clean
    200 with non-empty body, ``None`` otherwise. EBI returns 404 for
    un-parseable names; we treat 404 and 5xx identically here because
    the caller does not retry either.
    """
    base = web_url if web_url.endswith("/") else web_url + "/"
    encoded = urllib.parse.quote(name, safe="")
    url = f"{base}{encoded}.smi"

    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout,
        )
    except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
        logger.info(
            "opsin_web_network_error",
            extra={"backend": "web", "error_kind": type(exc).__name__},
        )
        return None

    if resp.status_code != 200:
        logger.info(
            "opsin_web_non_200",
            extra={"backend": "web", "status_code": resp.status_code},
        )
        return None

    smiles = resp.text.strip()
    return smiles or None


def iupac_to_smiles(
    name: str,
    backend: OpsinBackend = "local",
    *,
    subprocess_timeout: float = 10.0,
    web_timeout: float = 5.0,
    jar_path: str | None = None,
    web_url: str | None = None,
) -> str:
    """Resolve a chemical name to a SMILES string.

    Raises :class:`MoleculeGenerationError` with a user-facing Japanese
    message when no backend yields a result.

    ``jar_path`` and ``web_url`` default to the values on the singleton
    ``Settings`` (``OPSIN_JAR_PATH`` / ``OPSIN_WEB_URL`` env). The
    optional kwargs make this function unit-testable without touching
    process-global settings state.
    """
    if not name:
        raise MoleculeGenerationError(_FALLBACK_ERROR_MESSAGE)

    sanitized = name.replace("\x00", "").strip()
    if not sanitized:
        raise MoleculeGenerationError(_FALLBACK_ERROR_MESSAGE)
    if "\n" in sanitized or "\r" in sanitized:
        raise MoleculeGenerationError(_FALLBACK_ERROR_MESSAGE)

    alias_hit = _try_alias(sanitized)
    if alias_hit is not None:
        logger.info(
            "opsin_alias_hit",
            extra={"backend": "alias", "input_name_len": len(sanitized)},
        )
        return alias_hit

    settings = get_settings()
    effective_jar_path = jar_path if jar_path is not None else settings.OPSIN_JAR_PATH
    effective_web_url = web_url if web_url is not None else settings.OPSIN_WEB_URL

    if backend in ("local", "local_only"):
        resolved_jar = _resolve_jar_path(effective_jar_path)
        if resolved_jar is not None:
            smiles = _call_local(
                sanitized, jar_path=resolved_jar, timeout=subprocess_timeout
            )
            if smiles is not None:
                return smiles
        if backend == "local_only":
            raise MoleculeGenerationError(_FALLBACK_ERROR_MESSAGE)

    if backend in ("local", "web"):
        smiles = _call_web(
            sanitized, web_url=effective_web_url, timeout=web_timeout
        )
        if smiles is not None:
            return smiles

    raise MoleculeGenerationError(_FALLBACK_ERROR_MESSAGE)
