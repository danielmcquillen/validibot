"""Tests for the validation run detail page."""

from datetime import timedelta
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.tests.factories import ValidationRunFactory


class ValidationRunDetailViewTests(TestCase):
    """Exercise small but important run-detail presentation details."""

    def test_retained_file_submission_shows_filename_and_view_button(self):
        """Stored uploaded files should remain inspectable from run results."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org], username="daniel")
        grant_role(user, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        user.memberships.get(org=org).set_roles(
            {RoleCode.VALIDATION_RESULTS_VIEWER},
        )
        user.set_current_org(org)
        upload = SimpleUploadedFile(
            "visible.json",
            b'{"visible": true}',
            content_type="application/json",
        )
        submission = SubmissionFactory(
            org=org,
            user=user,
            project__org=org,
            retention_policy=SubmissionRetention.STORE_30_DAYS,
        )
        submission.set_content(
            uploaded_file=upload,
            filename="visible.json",
            file_type=SubmissionFileType.JSON,
        )
        submission.save()
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=user,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", kwargs={"pk": run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data file")
        self.assertContains(response, "visible.json")
        self.assertContains(response, 'data-bs-target="#submissionContentModal"')
        self.assertContains(response, 'id="submissionContentModal"')
        self.assertEqual(response.context["submission_content"], '{"visible": true}')
        self.assertTrue(response.context["submission_content_can_be_viewed"])

    def test_do_not_store_file_submission_keeps_filename_without_view_button(self):
        """No-store uploaded files should show retained metadata but no content UI."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org], username="daniel")
        grant_role(user, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        user.memberships.get(org=org).set_roles(
            {RoleCode.VALIDATION_RESULTS_VIEWER},
        )
        user.set_current_org(org)
        upload = SimpleUploadedFile(
            "private.json",
            b'{"private": true}',
            content_type="application/json",
        )
        submission = SubmissionFactory(
            org=org,
            user=user,
            project__org=org,
            retention_policy=SubmissionRetention.DO_NOT_STORE,
        )
        submission.set_content(
            uploaded_file=upload,
            filename="private.json",
            file_type=SubmissionFileType.JSON,
        )
        submission.save()
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=user,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", kwargs={"pk": run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data file")
        self.assertContains(response, "private.json")
        self.assertContains(
            response,
            "Submission content has been purged per retention policy "
            "and cannot be viewed.",
        )
        self.assertNotContains(response, 'data-bs-target="#submissionContentModal"')
        self.assertNotContains(response, 'id="submissionContentModal"')
        self.assertEqual(response.context["submission_content"], "")
        self.assertFalse(response.context["submission_content_can_be_viewed"])

    def test_expired_file_submission_keeps_filename_without_view_button(self):
        """Expired stored files should be hidden before the purge job deletes them."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org], username="daniel")
        grant_role(user, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        user.memberships.get(org=org).set_roles(
            {RoleCode.VALIDATION_RESULTS_VIEWER},
        )
        user.set_current_org(org)
        upload = SimpleUploadedFile(
            "expired.json",
            b'{"expired_secret": true}',
            content_type="application/json",
        )
        submission = SubmissionFactory(
            org=org,
            user=user,
            project__org=org,
            retention_policy=SubmissionRetention.STORE_1_DAY,
        )
        submission.set_content(
            uploaded_file=upload,
            filename="expired.json",
            file_type=SubmissionFileType.JSON,
        )
        submission.save()
        submission.expires_at = timezone.now() - timedelta(minutes=1)
        submission.save(update_fields=["expires_at"])
        self.assertTrue(
            submission.input_file.storage.exists(submission.input_file.name),
        )
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=user,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", kwargs={"pk": run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data file")
        self.assertContains(response, "expired.json")
        self.assertContains(
            response,
            "Submission content has been purged per retention policy "
            "and cannot be viewed.",
        )
        self.assertNotContains(response, 'data-bs-target="#submissionContentModal"')
        self.assertNotContains(response, 'id="submissionContentModal"')
        self.assertNotContains(response, "expired_secret")
        self.assertEqual(response.context["submission_content"], "")
        self.assertFalse(response.context["submission_content_can_be_viewed"])

    def test_retained_inline_submission_shows_data_view_button(self):
        """Inline exact data should use a data label instead of a filename label."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org], username="daniel")
        grant_role(user, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        user.memberships.get(org=org).set_roles(
            {RoleCode.VALIDATION_RESULTS_VIEWER},
        )
        user.set_current_org(org)
        submission = SubmissionFactory(
            org=org,
            user=user,
            project__org=org,
            content='{"inline": true}',
            original_filename="",
            retention_policy=SubmissionRetention.STORE_30_DAYS,
        )
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=user,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", kwargs={"pk": run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data")
        self.assertNotContains(response, "Data file")
        self.assertContains(response, 'data-bs-target="#submissionContentModal"')
        self.assertEqual(response.context["submission_content"], '{"inline": true}')
        self.assertTrue(response.context["submission_content_can_be_viewed"])

    def test_do_not_store_inline_submission_hides_content(self):
        """No-store inline data should be treated like no-store uploaded content."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org], username="daniel")
        grant_role(user, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        user.memberships.get(org=org).set_roles(
            {RoleCode.VALIDATION_RESULTS_VIEWER},
        )
        user.set_current_org(org)
        submission = SubmissionFactory(
            org=org,
            user=user,
            project__org=org,
            content='{"inline_secret": true}',
            original_filename="",
            retention_policy=SubmissionRetention.DO_NOT_STORE,
        )
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=user,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", kwargs={"pk": run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data")
        self.assertNotContains(response, "Data file")
        self.assertContains(
            response,
            "Submission content has been purged per retention policy "
            "and cannot be viewed.",
        )
        self.assertNotContains(response, 'data-bs-target="#submissionContentModal"')
        self.assertNotContains(response, "inline_secret")
        self.assertEqual(response.context["submission_content"], "")
        self.assertFalse(response.context["submission_content_can_be_viewed"])

    def test_submitter_uses_custom_name_field_without_none_none(self):
        """The detail view should not render Django's broken full-name fallback."""
        org = OrganizationFactory()
        user = UserFactory(
            orgs=[org],
            username="daniel",
            name="Daniel McQuillen",
        )
        grant_role(user, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        user.memberships.get(org=org).set_roles(
            {RoleCode.VALIDATION_RESULTS_VIEWER},
        )
        user.set_current_org(org)

        submission = SubmissionFactory(
            org=org,
            user=user,
            project__org=org,
        )
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=user,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", kwargs={"pk": run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "daniel")
        self.assertContains(response, "Daniel McQuillen")
        self.assertNotContains(response, "None None")

    def test_json_view_uses_app_json_viewer_layout(self):
        """The JSON page should render inside the normal app layout."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org], username="daniel")
        grant_role(user, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        user.memberships.get(org=org).set_roles(
            {RoleCode.VALIDATION_RESULTS_VIEWER},
        )
        user.set_current_org(org)

        submission = SubmissionFactory(
            org=org,
            user=user,
            project__org=org,
        )
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=user,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_json", kwargs={"pk": run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Validation Run JSON")
        self.assertContains(response, "json-viewer")
        self.assertContains(response, "json-viewer-root")
        breadcrumbs = response.context["breadcrumbs"]
        self.assertEqual(breadcrumbs[-2]["name"], f"Run #{run.pk}")
        self.assertEqual(breadcrumbs[-1]["name"], "JSON")

    def test_detail_shows_signed_resource_label_in_credential_card(self):
        """The credential card should show the signed submission label."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org], username="daniel")
        grant_role(user, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        user.memberships.get(org=org).set_roles(
            {RoleCode.VALIDATION_RESULTS_VIEWER},
        )
        user.set_current_org(org)

        submission = SubmissionFactory(
            org=org,
            user=user,
            project__org=org,
        )
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=user,
        )
        credential = SimpleNamespace(
            media_type="application/vc+jwt",
            created=run.created,
            kid="kid-123456",
            payload_json={
                "credentialSubject": {
                    "resourceLabel": "Product 1",
                },
            },
        )

        self.client.force_login(user)
        with (
            patch.dict("sys.modules", _fake_pro_modules(credential)),
            patch(
                "validibot.validations.credential_utils.apps.is_installed",
                return_value=True,
            ),
        ):
            response = self.client.get(
                reverse("validations:validation_detail", kwargs={"pk": run.pk}),
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Signed Credential")
        self.assertContains(response, "Product 1")


def _fake_pro_modules(credential):
    """Return a minimal validibot_pro module tree for community tests."""

    pro_module = ModuleType("validibot_pro")
    credentials_module = ModuleType("validibot_pro.credentials")
    models_module = ModuleType("validibot_pro.credentials.models")
    models_module.IssuedCredential = SimpleNamespace(
        objects=SimpleNamespace(
            filter=lambda **_kwargs: SimpleNamespace(first=lambda: credential),
        ),
    )
    return {
        "validibot_pro": pro_module,
        "validibot_pro.credentials": credentials_module,
        "validibot_pro.credentials.models": models_module,
    }
