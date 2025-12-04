"""
JWT token service using GCP KMS for signing.

This module creates JWT tokens for validator callbacks. Tokens are signed
using GCP KMS asymmetric keys, avoiding the need to store signing secrets
in the Django app.

Design: Simple functions for create/verify. No stateful objects.

Why GCP KMS:
- No need to store signing secrets in Django
- Keys are managed by GCP with audit logs
- Automatic key rotation support
- HSM-backed security (optional)
"""

import hashlib
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from google.cloud import kms

DEFAULT_KMS_KEY_VERSION = "1"


def _build_kms_crypto_key_version(
    kms_key_name: str,
    kms_key_version: str | None = None,
) -> str:
    """
    Construct the full cryptoKeyVersion resource path.

    If the caller passed a full version path (already contains /cryptoKeyVersions/),
    return it unchanged. Otherwise append the supplied version (default: "1").
    """
    normalized = kms_key_name.rstrip("/")
    segments = normalized.split("/")
    if "cryptoKeyVersions" in segments:
        return normalized

    version = kms_key_version or DEFAULT_KMS_KEY_VERSION
    return f"{normalized}/cryptoKeyVersions/{version}"


def create_callback_token(
    *,
    run_id: str,
    step_run_id: str,
    validator_id: str,
    org_id: str,
    kms_key_name: str,
    kms_key_version: str | None = None,
    expires_hours: int = 24,
) -> str:
    """
    Create a JWT callback token signed with GCP KMS.

    This function creates a JWT token containing run context and expiration time.
    The token is signed using a GCP KMS asymmetric signing key (RSA or ECDSA).

    Args:
        run_id: Validation run UUID
        step_run_id: ValidationStepRun UUID (identifies which step this token is for)
        validator_id: Validator UUID
        org_id: Organization UUID
        kms_key_name: Full KMS key path (projects/.../keyRings/.../cryptoKeys/...)
        kms_key_version: Optional KMS key version (default: "1" when omitted)
        expires_hours: Token expiration in hours (default: 24)

    Returns:
        JWT token string (header.payload.signature)

    Raises:
        google.cloud.exceptions.GoogleCloudError: If KMS signing fails

    Example:
        >>> token = create_callback_token(
        ...     run_id="abc-123",
        ...     step_run_id="step-456",
        ...     validator_id="val-789",
        ...     org_id="org-012",
        ...     kms_key_name=(
        ...         "projects/my-project/locations/us/"
        ...         "keyRings/validibot/cryptoKeys/callback-token"
        ...     ),
        ...     expires_hours=24,
        ... )
        >>> print(token[:20])  # eyJhbGciOiJSUzI1NiI...
    """
    # Create JWT header
    header = {
        "alg": "RS256",  # RSA signature with SHA-256
        "typ": "JWT",
    }

    # Create JWT payload with full context for security and routing
    now = datetime.now(UTC)
    payload = {
        "run_id": run_id,
        "step_run_id": step_run_id,
        "validator_id": validator_id,
        "org_id": org_id,
        "iat": int(now.timestamp()),  # Issued at
        "exp": int((now + timedelta(hours=expires_hours)).timestamp()),  # Expires
    }

    # Encode header and payload as base64url
    import base64

    def base64url_encode(data: dict) -> str:
        json_bytes = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(json_bytes).rstrip(b"=").decode()

    header_b64 = base64url_encode(header)
    payload_b64 = base64url_encode(payload)

    # Create signing input (header.payload)
    signing_input = f"{header_b64}.{payload_b64}"

    # Sign with KMS
    kms_client = kms.KeyManagementServiceClient()

    # Hash the signing input with SHA-256
    digest = hashlib.sha256(signing_input.encode()).digest()

    # Create the digest object for KMS
    digest_obj = {"sha256": digest}

    # Sign the digest using KMS
    crypto_key_version = _build_kms_crypto_key_version(
        kms_key_name,
        kms_key_version,
    )
    response = kms_client.asymmetric_sign(
        request={
            "name": crypto_key_version,
            "digest": digest_obj,
        }
    )

    # Encode signature as base64url
    signature_b64 = base64.urlsafe_b64encode(response.signature).rstrip(b"=").decode()

    # Return complete JWT
    return f"{signing_input}.{signature_b64}"


def verify_callback_token(
    token: str,
    kms_key_name: str,
    kms_key_version: str | None = None,
) -> dict:
    """
    Verify a JWT callback token using GCP KMS public key.

    This function verifies the token signature and extracts the payload.
    It checks expiration and validates the signature using KMS.

    Args:
        token: JWT token string (header.payload.signature)
        kms_key_name: Full KMS key path
        kms_key_version: Optional key version (default: "1" when omitted)

    Returns:
        Token payload dict with 'run_id', 'exp', etc.

    Raises:
        ValueError: If token format is invalid
        jwt.ExpiredSignatureError: If token is expired
        jwt.InvalidSignatureError: If signature is invalid

    Example:
        >>> payload = verify_callback_token(
        ...     token="eyJhbGciOiJ...",
        ...     kms_key_name=(
        ...         "projects/my-project/locations/us/"
        ...         "keyRings/validibot/cryptoKeys/callback-token"
        ...     ),
        ... )
        >>> print(payload["run_id"])
        'abc-123'
    """
    import base64

    # Split token into parts
    parts = token.split(".")
    if len(parts) != 3:  # noqa: PLR2004
        msg = "Invalid JWT format (expected header.payload.signature)"
        raise ValueError(msg)

    header_b64, payload_b64, signature_b64 = parts

    # Decode payload
    def base64url_decode(data: str) -> bytes:
        # Add padding if needed
        padding = 4 - (len(data) % 4)
        if padding != 4:  # noqa: PLR2004
            data += "=" * padding
        return base64.urlsafe_b64decode(data)

    payload_bytes = base64url_decode(payload_b64)
    payload = json.loads(payload_bytes)

    # Check expiration
    now = datetime.now(UTC)
    exp = datetime.fromtimestamp(payload["exp"], UTC)
    if now > exp:
        msg = f"Token expired at {exp}"
        raise ValueError(msg)

    # Verify signature with KMS
    signing_input = f"{header_b64}.{payload_b64}"
    signature_bytes = base64url_decode(signature_b64)

    # Get public key from KMS
    kms_client = kms.KeyManagementServiceClient()
    crypto_key_version = _build_kms_crypto_key_version(
        kms_key_name,
        kms_key_version,
    )
    public_key_response = kms_client.get_public_key(
        request={"name": crypto_key_version}
    )

    # Verify signature using cryptography library
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    # Parse the public key
    public_key_pem = public_key_response.pem.encode()
    public_key = serialization.load_pem_public_key(public_key_pem)

    # Verify the signature
    try:
        public_key.verify(
            signature_bytes,
            signing_input.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as e:
        msg = f"Invalid signature: {e}"
        raise ValueError(msg) from e

    return payload
