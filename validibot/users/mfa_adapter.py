"""Validibot MFA adapter — encrypts TOTP secrets and recovery-code seeds.

Allauth's default :class:`~allauth.mfa.adapter.DefaultMFAAdapter` has
no-op ``encrypt()``/``decrypt()`` methods, which means TOTP secrets and
recovery-code seed material are persisted to ``mfa_authenticator.data``
in cleartext. Anyone with read access to the database can then compute
any enrolled user's one-time codes. That's not an acceptable default for
Validibot, so we plug in this adapter via ``settings.MFA_ADAPTER``.

The adapter uses `Fernet <https://cryptography.io/en/latest/fernet/>`_ —
authenticated symmetric encryption based on AES-128-CBC + HMAC-SHA256
with a URL-safe base64 wire format. That's strictly better than AES-GCM
rolled by hand: tamper detection is built in (bad ciphertext or a bad
key raises :class:`~cryptography.fernet.InvalidToken` rather than
returning garbage), and the API is designed so you can't accidentally
reuse a nonce.

The encryption key comes from ``settings.MFA_ENCRYPTION_KEY`` (wired to
the ``DJANGO_MFA_ENCRYPTION_KEY`` env var), kept deliberately separate
from ``SECRET_KEY`` so that rotating Django's secret key — which breaks
sessions and signed cookies but not long-lived MFA enrollments — does
NOT invalidate every user's second factor and lock them out of their
accounts.

Key rotation: if you need to rotate the MFA encryption key, don't
overwrite ``MFA_ENCRYPTION_KEY``. Instead, use a :class:`MultiFernet`
(not wired up here; add it when needed) with the new key first and the
old key second — Fernet will decrypt with whichever key works and
re-encrypt under the new one on next write.
"""

from __future__ import annotations

from allauth.mfa.adapter import DefaultMFAAdapter
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class ValidibotMFAAdapter(DefaultMFAAdapter):
    """MFA adapter that encrypts secret material before it hits the DB.

    Symmetric encryption only — we need to be able to DECRYPT on every
    auth attempt to verify the user's code, so we can't use hashing.
    That's the nature of TOTP: the server has to know the secret in
    order to compute the expected 6-digit code. Encrypting at rest
    simply bumps the attack surface from "DB read access" to "DB read
    access AND the encryption key".
    """

    def _fernet(self) -> Fernet:
        """Return a Fernet instance built from the configured key.

        Raises :class:`~django.core.exceptions.ImproperlyConfigured` if
        the key is missing or malformed — better to fail loudly at
        first use than to silently fall back to no-op encryption.
        """
        key = getattr(settings, "MFA_ENCRYPTION_KEY", None)
        if not key:
            msg = (
                "MFA_ENCRYPTION_KEY is not configured. Set the "
                "DJANGO_MFA_ENCRYPTION_KEY environment variable to a "
                "Fernet key (base64-encoded 32 bytes; generate one with "
                "`python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'`)."
            )
            raise ImproperlyConfigured(msg)
        try:
            return Fernet(key if isinstance(key, bytes) else key.encode())
        except ValueError as exc:
            msg = (
                "MFA_ENCRYPTION_KEY is malformed — Fernet expects a "
                "URL-safe base64-encoded 32-byte key."
            )
            raise ImproperlyConfigured(msg) from exc

    def encrypt(self, text: str) -> str:
        """Encrypt ``text`` before allauth persists it to the DB.

        Called by allauth when storing TOTP secrets and recovery-code
        hashes. Returns a URL-safe ASCII string so the value fits in
        the existing ``JSONField`` column without migration.
        """
        return self._fernet().encrypt(text.encode()).decode()

    def decrypt(self, encrypted_text: str) -> str:
        """Decrypt a value that was previously written by :meth:`encrypt`.

        Raises :class:`~cryptography.fernet.InvalidToken` if the
        ciphertext is corrupt, was encrypted with a different key, or
        was stored in cleartext before this adapter was installed. We
        deliberately do NOT fall back to returning the input as-is on
        failure — that would silently accept tampered or unencrypted
        values, defeating the whole point of encrypting.

        If you're seeing InvalidToken after enabling this adapter on an
        existing database, see the migration in
        ``validibot/users/migrations/`` that wipes any pre-existing
        authenticators so they can be re-enrolled under the new key.
        """
        try:
            return self._fernet().decrypt(encrypted_text.encode()).decode()
        except InvalidToken as exc:
            msg = (
                "Failed to decrypt MFA secret — the stored value was "
                "either tampered with, encrypted under a different key, "
                "or is legacy cleartext from before the MFA adapter was "
                "enabled. Have affected users re-enroll their "
                "authenticator."
            )
            raise InvalidToken(msg) from exc
