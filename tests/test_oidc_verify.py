"""Tests for OIDC bearer-token verification (§5.2 of the design brief,
two-stage flow).

``google.oauth2.id_token.verify_oauth2_token`` is mocked, so these tests
need no network and no GCP credentials.
"""
from __future__ import annotations

from unittest import mock

import pytest

from app import oidc_verify
from app.oidc_verify import OIDCVerificationError


_AUDIENCE = "https://mol-slack-viewer-xxxxxxxx-an.a.run.app"
_PRINCIPAL = "molcast-ct-invoker@example-proj.iam.gserviceaccount.com"


def _verify(header: str | None):
    return oidc_verify.verify_oidc_token(
        header,
        expected_audience=_AUDIENCE,
        expected_principal=_PRINCIPAL,
    )


# ---------------------------------------------------------------------------
# Header shape
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("header", [None, "", "Token abc", "bearer abc"])
def test_missing_or_malformed_bearer_raises(header):
    """The case-sensitive ``Bearer `` prefix is required; anything
    else short-circuits before signature verification.
    """
    with pytest.raises(OIDCVerificationError):
        _verify(header)


def test_empty_token_raises():
    with pytest.raises(OIDCVerificationError):
        _verify("Bearer    ")


# ---------------------------------------------------------------------------
# google-auth verification
# ---------------------------------------------------------------------------
def test_verify_oauth2_token_failure_raises():
    """``google-auth`` raises ``ValueError`` for signature / expiry /
    audience mismatch — we surface the same error class without
    leaking detail.
    """
    with mock.patch.object(
        oidc_verify.id_token, "verify_oauth2_token", side_effect=ValueError("bad sig")
    ):
        with pytest.raises(OIDCVerificationError):
            _verify("Bearer xxx")


# ---------------------------------------------------------------------------
# Issuer & principal
# ---------------------------------------------------------------------------
def test_bad_issuer_raises():
    """A Google-signed token from a non-Google issuer endpoint
    (theoretically impossible but defended in depth) must fail.
    """
    with mock.patch.object(
        oidc_verify.id_token,
        "verify_oauth2_token",
        return_value={"iss": "https://example.com", "email": _PRINCIPAL},
    ):
        with pytest.raises(OIDCVerificationError):
            _verify("Bearer xxx")


def test_bad_principal_raises():
    """A token from a different Google service account is rejected;
    this is the guard against arbitrary Google clients calling
    ``/internal/process`` once they know the audience.
    """
    with mock.patch.object(
        oidc_verify.id_token,
        "verify_oauth2_token",
        return_value={
            "iss": "https://accounts.google.com",
            "email": "other@example-proj.iam.gserviceaccount.com",
        },
    ):
        with pytest.raises(OIDCVerificationError):
            _verify("Bearer xxx")


def test_valid_token_returns_claims():
    valid_claims = {
        "iss": "https://accounts.google.com",
        "email": _PRINCIPAL,
        "aud": _AUDIENCE,
    }
    with mock.patch.object(
        oidc_verify.id_token,
        "verify_oauth2_token",
        return_value=valid_claims,
    ):
        claims = _verify("Bearer xxx")
    assert claims == valid_claims


def test_short_issuer_form_accepted():
    """Google may emit ``"iss": "accounts.google.com"`` (no ``https://``);
    both forms are valid per OpenID Connect docs.
    """
    with mock.patch.object(
        oidc_verify.id_token,
        "verify_oauth2_token",
        return_value={"iss": "accounts.google.com", "email": _PRINCIPAL},
    ):
        claims = _verify("Bearer xxx")
    assert claims["iss"] == "accounts.google.com"
