# core/jwks.py
"""
JWKS (JSON Web Key Set) endpoint support.

Publishes public keys from Google Cloud KMS for credential verification.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Choose your advertised alg to match your KMS key:
# ES256 for ECC_NIST_P256, PS256 for RSA_2048 with PSS
JWKS_ALG = getattr(settings, "SV_JWKS_ALG", "ES256")


def jwk_from_kms_key(key_resource_name: str, alg: str) -> dict:
    """
    Convert Google Cloud KMS key to JWK format.

    Compatibility wrapper for the old function name.

    Args:
        key_resource_name: Full resource name of the GCP KMS key
        alg: Algorithm (e.g., ES256)

    Returns:
        JWK dictionary
    """
    from validibot.core.gcp_kms import jwk_from_gcp_key

    return jwk_from_gcp_key(key_resource_name, alg)


def get_jwks_keys() -> list[dict]:
    """
    Get all public keys to publish in JWKS.

    Returns:
        List of JWK dictionaries
    """
    keys = []

    # Google Cloud KMS keys
    gcp_keys = getattr(settings, "GCP_KMS_JWKS_KEYS", [])
    if not gcp_keys:
        logger.warning("No GCP_KMS_JWKS_KEYS configured")
        return keys

    from validibot.core.gcp_kms import jwk_from_gcp_key

    for key_name in gcp_keys:
        try:
            jwk = jwk_from_gcp_key(key_name, JWKS_ALG)
            keys.append(jwk)
        except Exception:
            # Log error but don't fail entire JWKS
            logger.exception(f"Failed to load GCP KMS key: {key_name}")

    return keys


def get_public_key_der(key_resource_name: str) -> bytes:
    """
    Fetch public key from Google Cloud KMS.

    Compatibility wrapper for the old function name.

    Args:
        key_resource_name: Full resource name of the GCP KMS key

    Returns:
        Public key in DER format
    """
    from validibot.core.gcp_kms import get_public_key_der as gcp_get_der

    return gcp_get_der(key_resource_name)


def kid_from_der(der: bytes) -> str:
    """Generate a key ID from DER-encoded public key."""
    from validibot.core.gcp_kms import kid_from_der as gcp_kid_from_der

    return gcp_kid_from_der(der)
