"""Tests for the base64 PEM decoder used by the OIDC signing key setting.

The ``_decode_idp_pem_from_env`` helper lives in
``config.settings.base`` and is used to decode the
``IDP_OIDC_PRIVATE_KEY_B64`` environment variable into the PEM-format
signing key required by django-allauth's OIDC provider. Storing the
key as base64 avoids multiline escaping headaches in .env files and
Secret Manager mounts.

The helper is pure Python (no Django required), so these tests don't
invoke ``django.setup()``.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from config.settings.base import _decode_idp_pem_from_env


def _generate_pem() -> str:
    """Generate a fresh RSA private key in PEM format for testing.

    Avoids embedding a static PEM string in the test file, which would
    trigger the ``detect-private-key`` pre-commit hook.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class TestDecodeIdpPemFromEnv:
    """Verify base64 → PEM decoding for the OIDC signing key setting."""

    def test_round_trip_with_generated_key(self):
        """Encoding a real PEM key then decoding should return the original.

        The core contract: whatever ``base64 < key.pem`` produces must
        round-trip back to the original PEM unchanged.
        """
        original = _generate_pem()
        encoded = base64.b64encode(original.encode("utf-8")).decode("ascii")
        result = _decode_idp_pem_from_env(encoded)
        assert result == original

    def test_empty_string_returns_empty(self):
        """An empty env var (key not configured) should return empty string.

        The default case for local dev where no OIDC key is configured.
        """
        assert _decode_idp_pem_from_env("") == ""

    def test_preserves_newlines(self):
        """The decoder must preserve newlines in the PEM output.

        The cryptography library requires proper PEM framing with
        newlines between the header, body lines, and footer.
        """
        original = _generate_pem()
        encoded = base64.b64encode(original.encode()).decode()
        result = _decode_idp_pem_from_env(encoded)
        assert "\n" in result
        assert "BEGIN" in result
        assert "END" in result

    def test_invalid_base64_raises(self):
        """Garbage input should raise a clear error, not silently return junk."""
        with pytest.raises(ValueError, match="Invalid"):
            _decode_idp_pem_from_env("!!!not-base64!!!")
