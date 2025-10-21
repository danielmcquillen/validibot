# core/jwks.py
import base64
from functools import lru_cache

import boto3
from authlib.jose import JsonWebKey
from cryptography.hazmat.primitives import serialization
from django.conf import settings

# Choose your advertised alg to match your KMS key:
# ES256 for ECC_NIST_P256, PS256 for RSA_2048 with PSS
JWKS_ALG = getattr(settings, "SV_JWKS_ALG", "ES256")


def _kms():
    return boto3.client(
        "kms", region_name=getattr(settings, "AWS_DEFAULT_REGION", None)
    )


@lru_cache(maxsize=64)
def get_public_key_der(key_id_or_alias: str) -> bytes:
    resp = _kms().get_public_key(KeyId=key_id_or_alias)
    return resp["PublicKey"]  # ASN.1 DER


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def kid_from_der(der: bytes) -> str:
    # Simple, stable kid: SHA-256 thumbprint of DER
    import hashlib

    return _b64url(hashlib.sha256(der).digest())


def jwk_from_kms_key(key_id_or_alias: str, alg: str):
    der = get_public_key_der(key_id_or_alias)
    public_key = serialization.load_der_public_key(der)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = JsonWebKey.import_key(pem).as_dict()
    public_jwk["use"] = "sig"
    public_jwk["alg"] = alg
    public_jwk["kid"] = kid_from_der(der)
    return public_jwk
