"""Tests for hashed personal API-key issuance and verification.

API keys are bearer credentials. These tests verify the properties the
database and service layer must preserve: plaintext is returned only at
issuance, storage uses keyed digests, stale keys are rejected, and rotation
revokes previous active keys.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from validibot.users.models import ValidibotAPIKey
from validibot.users.services.api_keys import issue_api_key
from validibot.users.services.api_keys import rotate_user_api_key
from validibot.users.services.api_keys import verify_api_key

pytestmark = pytest.mark.django_db

SHA256_HEX_LENGTH = 64


class TestAPIKeyIssuance:
    """Issuance tests protect the one-time-secret storage contract."""

    def test_issue_returns_plaintext_once_and_stores_only_digest(self, user):
        """The database must not contain the bearer secret after issuance."""

        issued = issue_api_key(user=user)

        issued.api_key.refresh_from_db()
        assert issued.full_key.startswith("vbk_1_")
        assert issued.api_key.public_id in issued.full_key
        assert len(issued.api_key.secret_digest) == SHA256_HEX_LENGTH
        assert issued.full_key not in issued.api_key.secret_digest
        assert issued.api_key.redacted_key == f"vbk_1_{issued.api_key.public_id}_..."

    def test_issued_key_parts_do_not_collide_with_separator(self, user):
        """Generated key parts must not contain ``_`` separator characters."""

        issued = issue_api_key(user=user)

        prefix, version, public_id, secret = issued.full_key.split("_")
        assert (prefix, version) == ("vbk", "1")
        assert public_id == issued.api_key.public_id
        assert "_" not in public_id
        assert "_" not in secret

    def test_default_expiry_is_recorded_for_new_keys(self, user):
        """New keys carry an expiry so forgotten credentials age out."""

        before = timezone.now()
        issued = issue_api_key(user=user)
        after = timezone.now()

        assert issued.api_key.expires_at is not None
        assert before + timedelta(days=364) < issued.api_key.expires_at
        assert issued.api_key.expires_at < after + timedelta(days=366)


class TestAPIKeyVerification:
    """Verification tests lock in fail-closed behavior for submitted keys."""

    def test_valid_key_returns_model_and_updates_last_used(self, user):
        """A correct key authenticates and records recent use metadata."""

        issued = issue_api_key(user=user)

        verified = verify_api_key(issued.full_key)

        assert verified == issued.api_key
        verified.refresh_from_db()
        assert verified.last_used_at is not None

    def test_wrong_secret_rejects(self, user):
        """Changing the secret part must fail even when public id matches."""

        issued = issue_api_key(user=user)
        bad_key = issued.full_key[:-1] + ("a" if issued.full_key[-1] != "a" else "b")

        assert verify_api_key(bad_key) is None

    def test_revoked_key_rejects(self, user):
        """Revocation must take effect immediately."""

        issued = issue_api_key(user=user)
        issued.api_key.revoked_at = timezone.now()
        issued.api_key.save(update_fields=["revoked_at"])

        assert verify_api_key(issued.full_key) is None

    def test_expired_key_rejects(self, user):
        """Expired keys must not authenticate even with a valid digest."""

        issued = issue_api_key(user=user)
        issued.api_key.expires_at = timezone.now() - timedelta(seconds=1)
        issued.api_key.save(update_fields=["expires_at"])

        assert verify_api_key(issued.full_key) is None

    def test_unknown_format_rejects_without_lookup(self):
        """Malformed or unsupported key formats fail closed."""

        assert verify_api_key("not-a-valid-key") is None
        assert verify_api_key("vbk_2_public_secret") is None


class TestAPIKeyRotation:
    """Rotation tests ensure old active keys stop working."""

    def test_rotation_revokes_existing_key_and_issues_replacement(self, user):
        """Only the replacement key remains usable after rotation."""

        first = issue_api_key(user=user)
        second = rotate_user_api_key(user=user)

        first.api_key.refresh_from_db()
        assert first.api_key.revoked_at is not None
        assert second.api_key.rotated_from == first.api_key
        assert verify_api_key(first.full_key) is None
        assert verify_api_key(second.full_key) == second.api_key
        assert (
            ValidibotAPIKey.objects.filter(
                user=user,
                revoked_at__isnull=True,
            ).count()
            == 1
        )
