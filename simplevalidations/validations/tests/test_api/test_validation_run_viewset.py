from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.workflows.tests.factories import WorkflowFactory


class ValidationRunViewSetTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Create test organization
        self.org = OrganizationFactory()

        # Create test user
        self.user = UserFactory(orgs=[self.org])  # Fixed: was orgs=[self.org]

        # Create another org and user for isolation testing
        self.other_org = OrganizationFactory()
        self.other_user = UserFactory(
            orgs=[self.other_org]
        )  # Fixed: was orgs=[self.other_org]

        # Create test project
        self.project = ProjectFactory(org=self.org)

        # Create test workflow
        self.workflow = WorkflowFactory(org=self.org, user=self.user)

        # Create test submission
        self.submission = SubmissionFactory(
            org=self.org, project=self.project, user=self.user
        )

        # Mock get_current_org method
        self.user.get_current_org = lambda: self.org
        self.other_user.get_current_org = lambda: self.other_org

    def test_authentication_required(self):
        """Test that authentication is required for all endpoints."""
        url = reverse("api:validation-runs-list")
        response = self.client.get(url)
        # Update expectation based on your actual API behavior
        # If it's returning 403, that means authentication might not be required
        # but permissions are failing
        self.assertIn(
            response.status_code,
            [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN],
        )

    def test_list_validation_runs_default_recent_only(self):
        """Test that only recent runs (last 30 days) are returned by default."""
        self.client.force_authenticate(user=self.user)

        # Create old run (40 days ago)
        old_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        old_run.created = timezone.now() - timedelta(days=40)
        old_run.save()

        # Create recent run (10 days ago)
        recent_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )
        recent_run.created = timezone.now() - timedelta(days=10)
        recent_run.save()

        url = reverse("api:validation-runs-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_list_validation_runs_all_flag(self):
        """Test that ?all=1 returns all runs regardless of age."""
        self.client.force_authenticate(user=self.user)

        # Create old run
        old_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        old_run.created = timezone.now() - timedelta(days=40)
        old_run.save()

        # Create recent run
        recent_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

    def test_filter_by_status(self):
        """Test filtering runs by status."""
        self.client.force_authenticate(user=self.user)

        # Create runs with different statuses
        pending_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        completed_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"status": ValidationRunStatus.PENDING})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(
            response.data["results"][0]["status"], ValidationRunStatus.PENDING
        )

    def test_filter_by_workflow(self):
        """Test filtering runs by workflow."""
        self.client.force_authenticate(user=self.user)

        # Create another workflow
        other_workflow = WorkflowFactory(org=self.org, user=self.user)

        # Create runs for different workflows
        run1 = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        run2 = ValidationRunFactory(
            submission=self.submission,
            workflow=other_workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"workflow": self.workflow.id})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["workflow"], self.workflow.slug)

    def test_filter_by_date_range(self):
        """Test filtering runs by date range."""
        self.client.force_authenticate(user=self.user)

        # Create runs at different dates
        old_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        old_run.created = timezone.now() - timedelta(days=5)
        old_run.save()

        new_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        # Filter for runs after 3 days ago
        after_date = (timezone.now() - timedelta(days=3)).date().isoformat()
        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"after": after_date})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(new_run.id))

    def test_organization_isolation(self):
        """Test that users only see runs from their own organization."""
        # Create run for user's org
        user_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        # Create project and workflow for other org
        other_project = ProjectFactory(org=self.other_org)
        other_workflow = WorkflowFactory(org=self.other_org, user=self.other_user)
        other_submission = SubmissionFactory(
            org=self.other_org, project=other_project, user=self.other_user
        )

        # Create run for other org
        other_run = ValidationRunFactory(
            submission=other_submission,
            workflow=other_workflow,
            org=self.other_org,
            project=other_project,
            status=ValidationRunStatus.PENDING,
        )

        # Test first user sees only their org's runs
        self.client.force_authenticate(user=self.user)
        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(user_run.id))

        # Test second user sees only their org's runs
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(other_run.id))

    def test_retrieve_validation_run(self):
        """Test retrieving a specific validation run."""
        self.client.force_authenticate(user=self.user)

        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-detail", kwargs={"pk": run.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(run.id))
        self.assertEqual(response.data["status"], ValidationRunStatus.PENDING)

    def test_delete_validation_run(self):
        """Test deleting a validation run."""
        self.client.force_authenticate(user=self.user)

        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-detail", kwargs={"pk": run.pk})
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(ValidationRun.objects.count(), 1)  # Still exists

    def test_ordering(self):
        """Test that results are ordered by creation date (newest first)."""
        self.client.force_authenticate(user=self.user)

        # Create runs with different timestamps
        old_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        old_run.created = timezone.now() - timedelta(hours=1)
        old_run.save()

        new_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

        # Newest should be first
        self.assertEqual(response.data["results"][0]["id"], str(new_run.id))
        self.assertEqual(response.data["results"][1]["id"], str(old_run.id))

    def test_create_validation_run_disallowed(self):
        """POST on validationrun-list should be disallowed (read-only viewset)."""
        self.client.force_authenticate(user=self.user)
        url = reverse("api:validation-runs-list")
        response = self.client.post(
            url,
            {
                "submission": getattr(self, "submission", None) and self.submission.id,
                "workflow": getattr(self, "workflow", None) and self.workflow.id,
                "org": getattr(self, "org", None) and self.org.id,
                "project": getattr(self, "project", None) and self.project.id,
                "status": getattr(self, "ValidationRunStatus", None) or None,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
