"""Google Cloud KMS integration for credential signing."""

import base64
import hashlib
from functools import lru_cache

from authlib.jose import JsonWebKey
from cryptography.hazmat.primitives import serialization
from google.cloud import kms


def _kms_client():
    """Get Google Cloud KMS client using Application Default Credentials."""
    return kms.KeyManagementServiceClient()


@lru_cache(maxsize=64)
def get_public_key_der(key_resource_name: str) -> bytes:
    """
    Fetch public key from Google Cloud KMS.

    Args:
        key_resource_name: Full resource name like
            "projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/KEY"

    Returns:
        Public key in DER format
    """
    client = _kms_client()

    # Get the primary version (latest active version)
    # Format: projects/*/locations/*/keyRings/*/cryptoKeys/*/cryptoKeyVersions/1
    key_version_name = f"{key_resource_name}/cryptoKeyVersions/1"

    response = client.get_public_key(request={"name": key_version_name})

    # Parse PEM to get DER
    public_key = serialization.load_pem_public_key(response.pem.encode())
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    return der


def _b64url(b: bytes) -> str:
    """Base64url encode bytes without padding."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def kid_from_der(der: bytes) -> str:
    """
    Generate key ID from DER public key.

    Uses SHA-256 thumbprint for stable, unique kid.
    """
    return _b64url(hashlib.sha256(der).digest())


def jwk_from_gcp_key(key_resource_name: str, alg: str) -> dict:
    """
    Convert Google Cloud KMS key to JWK format.

    Args:
        key_resource_name: Full KMS key resource name
        alg: Algorithm (e.g., "ES256")

    Returns:
        JWK dictionary
    """
    der = get_public_key_der(key_resource_name)

    # Load DER and convert to PEM
    public_key = serialization.load_der_public_key(der)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Use authlib to convert PEM to JWK
    public_jwk = JsonWebKey.import_key(pem).as_dict()

    # Add JWK metadata
    public_jwk["use"] = "sig"
    public_jwk["alg"] = alg
    public_jwk["kid"] = kid_from_der(der)

    return public_jwk


def sign_data(key_resource_name: str, data: bytes) -> bytes:
    """
    Sign data using Google Cloud KMS.

    Args:
        key_resource_name: Full KMS key resource name
        data: Data to sign

    Returns:
        Signature bytes
    """
    client = _kms_client()

    # EC keys require pre-hashed message
    digest = hashlib.sha256(data).digest()

    # Get primary version
    key_version_name = f"{key_resource_name}/cryptoKeyVersions/1"

    response = client.asymmetric_sign(
        request={
            "name": key_version_name,
            "digest": {"sha256": digest},
        }
    )

    return response.signature
