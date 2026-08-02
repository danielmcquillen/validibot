"""Public verification-key registry shared by community and Pro code.

The registry deliberately knows nothing about private keys or signing
providers. Pro converts a KMS or local key into a public JWK and calls the
registration function here; JWKS publication and credential verification read
the same durable records.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from validibot.core.models import CredentialVerificationKey

_PRIVATE_JWK_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
_PUBLIC_JWK_FIELDS = frozenset({"alg", "crv", "kid", "kty", "use", "x", "y"})


class CredentialVerificationKeyError(ValueError):
    """Raised when a candidate is not a safe public verification JWK."""


def validate_public_jwk(jwk: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized public JWK or reject private/unsupported material."""

    if not isinstance(jwk, dict):
        raise CredentialVerificationKeyError(
            "The verification key must be a JWK object."
        )

    private_fields = sorted(_PRIVATE_JWK_FIELDS.intersection(jwk))
    if private_fields:
        fields = ", ".join(private_fields)
        raise CredentialVerificationKeyError(
            f"Private JWK fields must never be stored: {fields}."
        )

    kid = jwk.get("kid")
    if not isinstance(kid, str) or not kid:
        raise CredentialVerificationKeyError("The public JWK must contain a kid.")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise CredentialVerificationKeyError(
            "Credential verification keys must be EC P-256 public keys."
        )
    if jwk.get("alg") != "ES256" or jwk.get("use") != "sig":
        raise CredentialVerificationKeyError(
            "Credential verification keys must declare alg=ES256 and use=sig."
        )
    if not all(isinstance(jwk.get(field), str) and jwk[field] for field in ("x", "y")):
        raise CredentialVerificationKeyError(
            "An EC public JWK must contain non-empty x and y coordinates."
        )

    unknown = sorted(set(jwk).difference(_PUBLIC_JWK_FIELDS))
    if unknown:
        fields = ", ".join(unknown)
        raise CredentialVerificationKeyError(
            f"Unsupported public JWK fields: {fields}."
        )

    return {key: jwk[key] for key in sorted(jwk)}


@transaction.atomic
def register_credential_verification_key(
    *,
    jwk: dict[str, Any],
    provider_reference: str = "",
) -> tuple[CredentialVerificationKey, bool]:
    """Idempotently register one public key without changing the active signer."""

    normalized = validate_public_jwk(jwk)
    kid = normalized["kid"]
    existing = (
        CredentialVerificationKey.objects.select_for_update().filter(kid=kid).first()
    )
    if existing is not None:
        if existing.jwk != normalized:
            raise CredentialVerificationKeyError(
                f"A different public JWK is already registered for kid {kid!r}."
            )
        if provider_reference and not existing.provider_reference:
            existing.provider_reference = provider_reference
            existing.save(update_fields=["provider_reference", "modified"])
        return existing, False

    return (
        CredentialVerificationKey.objects.create(
            kid=kid,
            jwk=normalized,
            provider_reference=provider_reference,
        ),
        True,
    )


def get_credential_verification_jwk(kid: str) -> dict[str, Any] | None:
    """Return the registered public JWK for ``kid``, if this instance owns it."""

    row = CredentialVerificationKey.objects.filter(kid=kid).only("jwk").first()
    return dict(row.jwk) if row is not None else None


def get_credential_verification_jwks() -> list[dict[str, Any]]:
    """Return every registered public JWK in stable registration order."""

    return [
        dict(jwk)
        for jwk in CredentialVerificationKey.objects.values_list("jwk", flat=True)
    ]


def is_credential_verification_key_registered(kid: str) -> bool:
    """Return whether the active or candidate signing key is published."""

    return CredentialVerificationKey.objects.filter(kid=kid).exists()


__all__ = [
    "CredentialVerificationKeyError",
    "get_credential_verification_jwk",
    "get_credential_verification_jwks",
    "is_credential_verification_key_registered",
    "register_credential_verification_key",
    "validate_public_jwk",
]
