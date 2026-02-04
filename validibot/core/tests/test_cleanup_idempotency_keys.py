"""
Tests for cleanup_idempotency_keys management command.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from validibot.core.models import IdempotencyKey
from validibot.users.tests.factories import OrganizationFactory


@pytest.fixture
def org(db):
    return OrganizationFactory()


@pytest.mark.django_db
class TestCleanupIdempotencyKeysCommand:
    """Tests for the cleanup_idempotency_keys management command."""

    def test_deletes_expired_keys(self, org):
        """Command deletes expired idempotency keys."""
        # Create expired key
        expired_key = IdempotencyKey.objects.create(
            org=org,
            key="expired-key",
            endpoint="test_endpoint",
            request_hash="hash1",
            expires_at=timezone.now() - timedelta(hours=1),
        )

        # Create non-expired key
        valid_key = IdempotencyKey.objects.create(
            org=org,
            key="valid-key",
            endpoint="test_endpoint",
            request_hash="hash2",
            expires_at=timezone.now() + timedelta(hours=23),
        )

        out = StringIO()
        call_command("cleanup_idempotency_keys", stdout=out)

        output = out.getvalue()
        assert "Deleted 1 expired idempotency key(s)" in output

        # Expired key should be gone
        assert not IdempotencyKey.objects.filter(pk=expired_key.pk).exists()
        # Valid key should remain
        assert IdempotencyKey.objects.filter(pk=valid_key.pk).exists()

    def test_dry_run_does_not_delete(self, org):
        """Dry run shows what would be deleted without actually deleting."""
        expired_key = IdempotencyKey.objects.create(
            org=org,
            key="expired-key",
            endpoint="test_endpoint",
            request_hash="hash1",
            expires_at=timezone.now() - timedelta(hours=1),
        )

        out = StringIO()
        call_command("cleanup_idempotency_keys", "--dry-run", stdout=out)

        output = out.getvalue()
        assert "[DRY RUN]" in output
        assert "Would delete 1 expired idempotency key(s)" in output

        # Key should still exist
        assert IdempotencyKey.objects.filter(pk=expired_key.pk).exists()

    def test_no_expired_keys_message(self, org):
        """Command shows appropriate message when no expired keys exist."""
        # Create only a valid key
        IdempotencyKey.objects.create(
            org=org,
            key="valid-key",
            endpoint="test_endpoint",
            request_hash="hash1",
            expires_at=timezone.now() + timedelta(hours=23),
        )

        out = StringIO()
        call_command("cleanup_idempotency_keys", stdout=out)

        output = out.getvalue()
        assert "No expired idempotency keys found" in output

    def test_empty_table_message(self):
        """Command handles empty table gracefully."""
        out = StringIO()
        call_command("cleanup_idempotency_keys", stdout=out)

        output = out.getvalue()
        assert "No expired idempotency keys found" in output

    def test_deletes_multiple_expired_keys(self, org):
        """Command deletes all expired keys in batch."""
        now = timezone.now()

        # Create multiple expired keys
        for i in range(5):
            IdempotencyKey.objects.create(
                org=org,
                key=f"expired-key-{i}",
                endpoint="test_endpoint",
                request_hash=f"hash{i}",
                expires_at=now - timedelta(hours=i + 1),
            )

        # Create one valid key
        IdempotencyKey.objects.create(
            org=org,
            key="valid-key",
            endpoint="test_endpoint",
            request_hash="valid",
            expires_at=now + timedelta(hours=23),
        )

        out = StringIO()
        call_command("cleanup_idempotency_keys", stdout=out)

        output = out.getvalue()
        assert "Deleted 5 expired idempotency key(s)" in output
        assert IdempotencyKey.objects.count() == 1
