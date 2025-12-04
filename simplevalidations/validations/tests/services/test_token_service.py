"""
Tests for token_service helpers.
"""

from simplevalidations.validations.services.cloud_run import token_service


def test_build_kms_crypto_key_version_explicit_path():
    """Should return unchanged path when a full cryptoKeyVersion is provided."""
    full_path = "projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/2"
    result = token_service._build_kms_crypto_key_version(  # noqa: SLF001
        full_path,
        kms_key_version="5",
    )
    assert result == full_path


def test_build_kms_crypto_key_version_default_version():
    """Should append default version when none is supplied."""
    key_name = "projects/p/locations/l/keyRings/r/cryptoKeys/k"
    result = token_service._build_kms_crypto_key_version(  # noqa: SLF001
        key_name,
    )
    assert result == f"{key_name}/cryptoKeyVersions/1"


def test_build_kms_crypto_key_version_custom_version():
    """Should append the provided version when supplied."""
    key_name = "projects/p/locations/l/keyRings/r/cryptoKeys/k"
    result = token_service._build_kms_crypto_key_version(  # noqa: SLF001
        key_name,
        kms_key_version="3",
    )
    assert result == f"{key_name}/cryptoKeyVersions/3"
