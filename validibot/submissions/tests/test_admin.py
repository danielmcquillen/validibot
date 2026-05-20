"""
Tests for retention-aware Submission admin behavior.

The Django admin is an operator UI surface, so these tests make sure raw
submitted data fields are hidden there whenever retention policy says users
must not be able to view the content, even if the underlying file still exists.
"""

from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test import TestCase
from django.utils import timezone

from validibot.submissions.admin import SubmissionAdmin
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.models import Submission
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import UserFactory


class SubmissionAdminRetentionTests(TestCase):
    """Cover admin form field visibility for retained submission content."""

    def setUp(self):
        """Build the admin instance and request used by field-visibility checks."""
        self.admin = SubmissionAdmin(Submission, AdminSite())
        self.request = RequestFactory().get("/")
        self.request.user = UserFactory(is_staff=True, is_superuser=True)

    def _file_submission(self, retention_policy: str) -> Submission:
        """Create a file-backed submission whose stored file still exists."""
        upload = SimpleUploadedFile(
            "admin-private.json",
            b'{"admin_private": true}',
            content_type="application/json",
        )
        submission = SubmissionFactory(retention_policy=retention_policy)
        submission.set_content(
            uploaded_file=upload,
            filename="admin-private.json",
            file_type=SubmissionFileType.JSON,
        )
        submission.save()
        self.assertTrue(
            submission.input_file.storage.exists(submission.input_file.name),
        )
        return submission

    def test_admin_hides_do_not_store_content_fields_while_file_exists(self):
        """No-store submissions should not expose raw admin form fields."""
        submission = self._file_submission(SubmissionRetention.DO_NOT_STORE)

        excluded = self.admin.get_exclude(self.request, submission)

        self.assertIn("content", excluded)
        self.assertIn("input_file", excluded)

    def test_admin_hides_expired_content_fields_while_file_exists(self):
        """Expired submissions should not expose raw admin form fields."""
        submission = self._file_submission(SubmissionRetention.STORE_1_DAY)
        submission.expires_at = timezone.now() - timedelta(minutes=1)
        submission.save(update_fields=["expires_at"])

        excluded = self.admin.get_exclude(self.request, submission)

        self.assertIn("content", excluded)
        self.assertIn("input_file", excluded)

    def test_admin_keeps_retained_content_fields_before_expiry(self):
        """Retained, unexpired submissions remain available to privileged admins."""
        submission = self._file_submission(SubmissionRetention.STORE_1_DAY)

        excluded = self.admin.get_exclude(self.request, submission) or ()

        self.assertNotIn("content", excluded)
        self.assertNotIn("input_file", excluded)
