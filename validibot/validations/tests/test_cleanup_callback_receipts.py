"""
Tests for the cleanup_callback_receipts management command.

The cleanup command deletes old CallbackReceipt records that are past
the retention period, helping keep the database clean while allowing
debugging of recent callbacks.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from validibot.validations.models import CallbackReceipt
from validibot.validations.tests.factories import CallbackReceiptFactory


@pytest.mark.django_db
class TestCleanupCallbackReceipts:
    """Tests for cleanup_callback_receipts management command."""

    def test_cleanup_deletes_old_receipts(self):
        """Command should delete receipts older than retention period."""
        # Create a receipt from 45 days ago
        old_receipt = CallbackReceiptFactory()
        old_time = timezone.now() - timedelta(days=45)
        CallbackReceipt.objects.filter(id=old_receipt.id).update(received_at=old_time)

        # Create a recent receipt (should not be deleted)
        recent_receipt = CallbackReceiptFactory()

        # Run cleanup with default 30 days
        out = StringIO()
        call_command("cleanup_callback_receipts", stdout=out)

        # Verify only old receipt was deleted
        assert not CallbackReceipt.objects.filter(id=old_receipt.id).exists()
        assert CallbackReceipt.objects.filter(id=recent_receipt.id).exists()
        assert "Deleted 1" in out.getvalue()

    def test_cleanup_respects_custom_days_parameter(self):
        """Command should use custom days parameter for cutoff."""
        # Create a receipt from 15 days ago
        receipt = CallbackReceiptFactory()
        time_15_days_ago = timezone.now() - timedelta(days=15)
        CallbackReceipt.objects.filter(id=receipt.id).update(received_at=time_15_days_ago)

        # With default 30 days, receipt should survive
        out = StringIO()
        call_command("cleanup_callback_receipts", stdout=out)
        assert CallbackReceipt.objects.filter(id=receipt.id).exists()

        # With 10 days, receipt should be deleted
        out = StringIO()
        call_command("cleanup_callback_receipts", "--days=10", stdout=out)
        assert not CallbackReceipt.objects.filter(id=receipt.id).exists()

    def test_cleanup_dry_run_does_not_delete(self):
        """Dry run mode should report but not delete receipts."""
        # Create an old receipt
        receipt = CallbackReceiptFactory()
        old_time = timezone.now() - timedelta(days=45)
        CallbackReceipt.objects.filter(id=receipt.id).update(received_at=old_time)

        # Run with --dry-run
        out = StringIO()
        call_command("cleanup_callback_receipts", "--dry-run", stdout=out)

        # Receipt should still exist
        assert CallbackReceipt.objects.filter(id=receipt.id).exists()
        assert "DRY RUN" in out.getvalue()
        assert "Would delete" in out.getvalue()

    def test_cleanup_no_receipts_found(self):
        """Command should report when no old receipts exist."""
        # Create only a recent receipt
        CallbackReceiptFactory()

        out = StringIO()
        call_command("cleanup_callback_receipts", stdout=out)

        assert "No callback receipts" in out.getvalue()

    def test_cleanup_with_no_receipts_at_all(self):
        """Command should handle empty table gracefully."""
        out = StringIO()
        call_command("cleanup_callback_receipts", stdout=out)

        assert "No callback receipts" in out.getvalue()

    def test_cleanup_multiple_old_receipts(self):
        """Command should delete all receipts past the cutoff."""
        old_time = timezone.now() - timedelta(days=60)

        # Create 5 old receipts
        old_receipts = []
        for _ in range(5):
            receipt = CallbackReceiptFactory()
            old_receipts.append(receipt.id)
        CallbackReceipt.objects.filter(id__in=old_receipts).update(received_at=old_time)

        # Create 2 recent receipts
        recent_receipts = [CallbackReceiptFactory() for _ in range(2)]

        out = StringIO()
        call_command("cleanup_callback_receipts", stdout=out)

        # All old receipts deleted
        assert CallbackReceipt.objects.filter(id__in=old_receipts).count() == 0
        # Recent receipts preserved
        assert CallbackReceipt.objects.filter(
            id__in=[r.id for r in recent_receipts]
        ).count() == 2  # noqa: PLR2004
        assert "Deleted 5" in out.getvalue()
