from http import HTTPStatus

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.urls import reverse

from simplevalidations.core import jwks


def _sample_der_public_key() -> bytes:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_jwk_from_kms_key_builds_expected_fields(monkeypatch):
    der = _sample_der_public_key()

    # Mock at gcp_kms level instead of jwks level
    monkeypatch.setattr(
        "simplevalidations.core.gcp_kms.get_public_key_der",
        lambda key: der,
    )

    test_key = "projects/test/locations/us/keyRings/test/cryptoKeys/test"
    public_jwk = jwks.jwk_from_kms_key(test_key, "ES256")

    assert public_jwk["kty"] == "EC"
    assert public_jwk["alg"] == "ES256"
    assert public_jwk["use"] == "sig"
    assert public_jwk["kid"] == jwks.kid_from_der(der)


@pytest.mark.django_db
def test_jwks_view_returns_expected_payload(client, settings, monkeypatch):
    # Use GCP KMS setting names
    test_key_name = "projects/test/locations/us/keyRings/test/cryptoKeys/test"
    settings.GCP_KMS_JWKS_KEYS = [test_key_name]
    expected_key = {"kty": "EC", "alg": "ES256", "use": "sig", "kid": "kid1"}

    def fake_jwk_from_kms_key(key_id, alg):
        assert key_id == test_key_name
        assert alg == settings.SV_JWKS_ALG
        return expected_key

    monkeypatch.setattr(
        "simplevalidations.core.views.jwk_from_kms_key",
        fake_jwk_from_kms_key,
    )

    response = client.get(reverse("jwks"))

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"keys": [expected_key]}
    assert response["Content-Type"] == "application/jwk-set+json"
    assert response["Cache-Control"] == "public, max-age=900"
