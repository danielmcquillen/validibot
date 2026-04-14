"""Tests for :class:`validibot.users.mfa_adapter.ValidibotMFAAdapter`.

We test the adapter in isolation — construct it directly, call
``encrypt()``/``decrypt()``, assert on outputs — rather than going
through allauth's TOTP flow. That keeps these tests fast and focused:
if allauth changes how/when it calls the adapter, this suite still
pins the cryptographic behaviour we care about.

What we cover:

- Round-trip: a value encrypted then decrypted returns the original
  string byte-for-byte.
- Ciphertext changes between encryptions of the same plaintext (Fernet
  embeds a fresh IV per ``encrypt()`` call, so output is
  non-deterministic even for identical input — important for
  not leaking "this user's secret didn't change" through ciphertext
  equality).
- Tampered ciphertext raises ``InvalidToken`` rather than silently
  returning garbage or the original input.
- Cleartext from the pre-encryption era is rejected (not accepted as a
  fallback). This pins the "no silent downgrade" property — if the
  adapter ever regresses to accepting cleartext, every migration-wiped
  user would reappear insecure.
- Missing/malformed ``MFA_ENCRYPTION_KEY`` raises ``ImproperlyConfigured``
  loudly, so a misconfigured deploy fails at first use rather than
  silently running with broken encryption.

We deliberately don't re-test Fernet itself (cryptography.io has its
own test suite); we test the Validibot-specific glue.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from validibot.users.mfa_adapter import ValidibotMFAAdapter

# A fresh key for this module. We don't reuse the test settings key so
# these tests are fully self-contained — swapping the settings module
# shouldn't break them.
VALID_KEY = Fernet.generate_key().decode()


class TestEncryptDecryptRoundtrip:
    """``encrypt()`` + ``decrypt()`` is a symmetric pair."""

    def test_roundtrip_preserves_plaintext(self):
        """Encrypt then decrypt a TOTP-shaped secret and get the original back.

        If this fails, every MFA verification would reject the user's
        correct code — total breakage.
        """
        plaintext = "JBSWY3DPEHPK3PXP"  # Example TOTP secret from RFC 6238.
        with override_settings(MFA_ENCRYPTION_KEY=VALID_KEY):
            adapter = ValidibotMFAAdapter()
            ciphertext = adapter.encrypt(plaintext)
            recovered = adapter.decrypt(ciphertext)
        assert recovered == plaintext

    def test_ciphertext_differs_between_encryptions(self):
        """Two calls to ``encrypt()`` on the same plaintext return different
        ciphertexts — Fernet embeds a fresh IV every call.

        This matters because equal ciphertext would let an observer with
        DB access say "user A's secret equals user B's secret" or "this
        user's TOTP secret didn't change after the rotation we thought
        rotated it." Non-determinism prevents those side-channels.
        """
        plaintext = "JBSWY3DPEHPK3PXP"
        with override_settings(MFA_ENCRYPTION_KEY=VALID_KEY):
            adapter = ValidibotMFAAdapter()
            a = adapter.encrypt(plaintext)
            b = adapter.encrypt(plaintext)
        assert a != b

    def test_roundtrip_handles_non_ascii(self):
        """Fernet operates on bytes, but our adapter takes/returns ``str``.

        allauth serialises recovery-code seed material to hex strings,
        so non-ASCII is unlikely in practice — but we'd rather have the
        guarantee than silently corrupt the one day it isn't ASCII.
        """
        plaintext = "secret-with-πñ-and-🗝️"
        with override_settings(MFA_ENCRYPTION_KEY=VALID_KEY):
            adapter = ValidibotMFAAdapter()
            recovered = adapter.decrypt(adapter.encrypt(plaintext))
        assert recovered == plaintext


class TestDecryptionFailureModes:
    """``decrypt()`` refuses to silently accept bad input."""

    def test_tampered_ciphertext_raises_invalid_token(self):
        """Flip one character in a valid ciphertext and decryption must fail.

        Fernet's HMAC catches any modification — if this regresses, an
        attacker with DB write access could feed arbitrary plaintext to
        the TOTP verifier.
        """
        with override_settings(MFA_ENCRYPTION_KEY=VALID_KEY):
            adapter = ValidibotMFAAdapter()
            ciphertext = adapter.encrypt("JBSWY3DPEHPK3PXP")
            # Flip a character in the middle (avoid the version byte at
            # position 0 which would change the failure mode).
            tampered = (
                ciphertext[:10]
                + ("X" if ciphertext[10] != "X" else "Y")
                + ciphertext[11:]
            )
            with pytest.raises(InvalidToken):
                adapter.decrypt(tampered)

    def test_legacy_cleartext_is_rejected(self):
        """A plain string that was never encrypted raises ``InvalidToken``.

        This is the key guarantee: after enabling the adapter, the
        0003_wipe_pre_encryption_authenticators migration should have
        cleared every legacy row. If a stray one remained, this
        behaviour ensures it's detected loudly rather than silently
        accepted as unencrypted — which would defeat the whole point of
        the adapter.
        """
        with override_settings(MFA_ENCRYPTION_KEY=VALID_KEY):
            adapter = ValidibotMFAAdapter()
            with pytest.raises(InvalidToken):
                adapter.decrypt("JBSWY3DPEHPK3PXP")

    def test_ciphertext_from_different_key_is_rejected(self):
        """A ciphertext encrypted under key A can't be decrypted by key B.

        Pins the "rotating the key locks out pre-rotation ciphertexts"
        behaviour, which is the point of having a dedicated MFA key —
        it also means rotation requires the ``MultiFernet`` dance
        documented in the runbook rather than a naive swap.
        """
        other_key = Fernet.generate_key().decode()
        with override_settings(MFA_ENCRYPTION_KEY=other_key):
            other_adapter = ValidibotMFAAdapter()
            ciphertext_under_other_key = other_adapter.encrypt("secret")
        with override_settings(MFA_ENCRYPTION_KEY=VALID_KEY):
            our_adapter = ValidibotMFAAdapter()
            with pytest.raises(InvalidToken):
                our_adapter.decrypt(ciphertext_under_other_key)


class TestMissingOrMalformedKey:
    """Adapter refuses to operate without a valid ``MFA_ENCRYPTION_KEY``.

    We want loud failures, not silent downgrades to "identity
    encryption" (i.e. allauth's default no-op adapter behaviour). A
    misconfigured deploy that quietly stored cleartext would be worse
    than one that refused to start.
    """

    def test_missing_key_raises_improperly_configured(self):
        """``encrypt()`` without the setting must fail noisily."""
        with override_settings(MFA_ENCRYPTION_KEY=None):
            adapter = ValidibotMFAAdapter()
            with pytest.raises(ImproperlyConfigured):
                adapter.encrypt("secret")

    def test_empty_key_raises_improperly_configured(self):
        """An empty-string key is treated the same as a missing key."""
        with override_settings(MFA_ENCRYPTION_KEY=""):
            adapter = ValidibotMFAAdapter()
            with pytest.raises(ImproperlyConfigured):
                adapter.encrypt("secret")

    def test_malformed_key_raises_improperly_configured(self):
        """A key that isn't a valid Fernet key (wrong length, non-base64)
        must fail with a clear message — not a mysterious ValueError
        from deep inside cryptography.
        """
        with override_settings(MFA_ENCRYPTION_KEY="not-a-valid-fernet-key"):
            adapter = ValidibotMFAAdapter()
            with pytest.raises(ImproperlyConfigured):
                adapter.encrypt("secret")

    def test_decrypt_also_fails_loudly_on_missing_key(self):
        """Same loud-failure guarantee on the decrypt path — we shouldn't
        have one code path that fails-closed and another that silently
        returns garbage.
        """
        with override_settings(MFA_ENCRYPTION_KEY=None):
            adapter = ValidibotMFAAdapter()
            with pytest.raises(ImproperlyConfigured):
                adapter.decrypt("anything")
