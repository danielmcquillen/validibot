"""Tests for signed credential naming helpers."""

from django.test import SimpleTestCase

from validibot.validations.credential_utils import (
    build_signed_credential_download_filename,
)
from validibot.validations.credential_utils import (
    extract_signed_credential_resource_label,
)
from validibot.validations.credential_utils import resolve_submission_resource_label


class ResolveSubmissionResourceLabelTests(SimpleTestCase):
    """Verify how signed credential resource labels are resolved."""

    digest_value = "42065c7498736fbc6ff4300ca3707e158821f7c84cba4a761aa1ee4e6b9ebe180"

    def test_prefers_submission_name(self):
        """An explicit submission name should become the signed label."""
        submission = type(
            "SubmissionStub",
            (),
            {
                "name": "Product 1",
                "original_filename": "product-1.json",
                "checksum_sha256": "a" * 64,
            },
        )()

        self.assertEqual(resolve_submission_resource_label(submission), "Product 1")

    def test_falls_back_to_original_filename(self):
        """The original filename should be used when no display name exists."""
        submission = type(
            "SubmissionStub",
            (),
            {
                "name": "",
                "original_filename": "product-1.json",
                "checksum_sha256": "a" * 64,
            },
        )()

        self.assertEqual(
            resolve_submission_resource_label(submission),
            "product-1.json",
        )

    def test_uses_digest_prefix_when_no_name_or_filename_exists(self):
        """A short digest-based label should be used as the last resort."""
        submission = type(
            "SubmissionStub",
            (),
            {
                "name": "",
                "original_filename": "",
                "checksum_sha256": self.digest_value,
            },
        )()

        self.assertEqual(
            resolve_submission_resource_label(submission),
            "Submission 42065c74",
        )


class SignedCredentialDownloadFilenameTests(SimpleTestCase):
    """Verify filesystem-friendly credential download names."""

    def test_uses_truncated_slugified_resource_label(self):
        """The download filename should derive from the signed human label."""
        filename = build_signed_credential_download_filename(
            resource_label="Product 1",
            workflow_slug="test-validation",
            fallback_identifier="1234",
        )

        self.assertEqual(
            filename,
            "product-1__test-validation__signed-credential.jwt",
        )

    def test_falls_back_when_slugify_strips_everything(self):
        """A fallback segment should be used when no safe label can be built."""
        filename = build_signed_credential_download_filename(
            resource_label="!!!",
            workflow_slug="test-validation",
            fallback_identifier="abcd-1234",
        )

        self.assertEqual(
            filename,
            "submission-abcd-1234__test-validation__signed-credential.jwt",
        )


class ExtractSignedCredentialResourceLabelTests(SimpleTestCase):
    """Verify extraction of the signed label from decoded payload JSON."""

    def test_returns_none_when_label_is_absent(self):
        """Missing resourceLabel should not break callers."""
        payload = {"credentialSubject": {}}

        self.assertIsNone(extract_signed_credential_resource_label(payload))

    def test_returns_trimmed_label_when_present(self):
        """The extracted label should be trimmed before display."""
        payload = {
            "credentialSubject": {
                "resourceLabel": "  Product 1  ",
            },
        }

        self.assertEqual(
            extract_signed_credential_resource_label(payload),
            "Product 1",
        )
