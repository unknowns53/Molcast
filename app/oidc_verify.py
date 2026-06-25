r"""OIDC token verification for ``POST /internal/process`` (see §5.2 of
the design brief, two-stage flow).

The endpoint is invoked by Cloud Tasks with an OIDC bearer token issued
on behalf of ``settings.TASKS_INVOKER_SA``. Cloud Run's
``allow-unauthenticated`` is retained so Slack can still POST
``/slack/mol`` directly; ``/internal/process`` is the only handler that
applies app-layer auth, and it MUST be the only path that exposes
``iupac_to_smiles`` + Firestore writes via background dispatch.

Verification consists of three checks:

  1. Bearer header well-formed.
  2. ``id_token.verify_oauth2_token`` succeeds (signature, expiry,
     audience). ``google-auth`` is already a transitive dependency of
     ``google-cloud-firestore`` and ``google-cloud-tasks``.
  3. ``iss`` is a Google OIDC issuer and ``email`` matches the
     configured invoker service account. Without (3) any Google account
     could mint a token with the right audience and reach the handler.
"""
from __future__ import annotations

import logging
from typing import Any

from google.auth.transport import requests as g_requests
from google.oauth2 import id_token


logger = logging.getLogger("molcast.oidc")


_VALID_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class OIDCVerificationError(Exception):
    """Raised on any failure of the three-step bearer-token check.

    The handler maps this to HTTP 403 without echoing the failure
    detail; the actual reason is logged with ``error_kind`` for
    operators.
    """


def verify_oidc_token(
    auth_header: str | None,
    *,
    expected_audience: str,
    expected_principal: str,
) -> dict[str, Any]:
    """Validate a Cloud-Tasks-issued OIDC bearer token.

    Raises :class:`OIDCVerificationError` on any failure. Returns the
    verified claims dict on success.
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.info("oidc_missing_bearer", extra={"error_kind": "MissingBearer"})
        raise OIDCVerificationError("missing bearer")

    token = auth_header[len("Bearer ") :].strip()
    if not token:
        logger.info("oidc_empty_token", extra={"error_kind": "EmptyToken"})
        raise OIDCVerificationError("empty token")

    try:
        claims = id_token.verify_oauth2_token(
            token, g_requests.Request(), audience=expected_audience
        )
    except ValueError as exc:
        # google-auth raises ValueError for signature / expiry /
        # audience mismatch — opaque on purpose.
        logger.info(
            "oidc_verify_failed",
            extra={"error_kind": type(exc).__name__},
        )
        raise OIDCVerificationError("verify_oauth2_token failed") from exc

    issuer = claims.get("iss")
    if issuer not in _VALID_ISSUERS:
        logger.info("oidc_bad_issuer", extra={"error_kind": "BadIssuer"})
        raise OIDCVerificationError("bad issuer")

    email = claims.get("email")
    if email != expected_principal:
        logger.info("oidc_bad_principal", extra={"error_kind": "BadPrincipal"})
        raise OIDCVerificationError("bad principal")

    return claims
