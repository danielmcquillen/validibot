"""Tests for the permanent public credential-verification key registry.

The registry is the application-side half of safe signing-key rotation. These
tests prove that only public ES256 material can be stored, registration is
idempotent, and historical keys remain available together for JWKS publication
and local verification.
"""

from __future__ import annotations

import pytest

from validibot.core.credential_keys import CredentialVerificationKeyError
from validibot.core.credential_keys import get_credential_verification_jwk
from validibot.core.credential_keys import get_credential_verification_jwks
from validibot.core.credential_keys import register_credential_verification_key
from validibot.core.models import CredentialVerificationKey

pytestmark = pytest.mark.django_db


def _public_jwk(kid: str, coordinate: str) -> dict[str, str]:
    """Return a small structurally valid public ES256 JWK for registry tests."""

    return {
        "kty": "EC",
        "crv": "P-256",
        "x": coordinate,
        "y": coordinate[::-1],
        "use": "sig",
        "alg": "ES256",
        "kid": kid,
    }


def test_registration_is_idempotent_and_preserves_one_row() -> None:
    """Re-running an operator command must not duplicate a published key."""

    first, first_created = register_credential_verification_key(
        jwk=_public_jwk("key-a", "a" * 43),
        provider_reference="projects/p/cryptoKeyVersions/1",
    )
    second, second_created = register_credential_verification_key(
        jwk=_public_jwk("key-a", "a" * 43),
        provider_reference="projects/p/cryptoKeyVersions/1",
    )

    assert first_created is True
    assert second_created is False
    assert second.pk == first.pk
    assert CredentialVerificationKey.objects.count() == 1


def test_registry_returns_current_and_historical_public_keys() -> None:
    """Ordinary rotation adds to JWKS instead of replacing the previous key."""

    register_credential_verification_key(jwk=_public_jwk("key-a", "a" * 43))
    register_credential_verification_key(jwk=_public_jwk("key-b", "b" * 43))

    assert [jwk["kid"] for jwk in get_credential_verification_jwks()] == [
        "key-a",
        "key-b",
    ]
    assert get_credential_verification_jwk("key-a")["kid"] == "key-a"
    assert get_credential_verification_jwk("unknown") is None


def test_registration_rejects_private_key_material() -> None:
    """A command bug must never copy a private scalar into the database."""

    candidate = _public_jwk("key-a", "a" * 43)
    candidate["d"] = "private"

    with pytest.raises(CredentialVerificationKeyError, match="Private JWK fields"):
        register_credential_verification_key(jwk=candidate)


def test_existing_kid_cannot_be_rebound_to_different_public_material() -> None:
    """The JOSE key identifier must remain a stable public-key identity."""

    register_credential_verification_key(jwk=_public_jwk("key-a", "a" * 43))

    with pytest.raises(CredentialVerificationKeyError, match="different public JWK"):
        register_credential_verification_key(jwk=_public_jwk("key-a", "b" * 43))
