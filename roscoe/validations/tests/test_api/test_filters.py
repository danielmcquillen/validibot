from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from roscoe.projects.tests.factories import ProjectFactory
from roscoe.submissions.tests.factories import SubmissionFactory
from roscoe.users.tests.factories import OrganizationFactory, UserFactory
from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.tests.factories import ValidationRunFactory
from roscoe.workflows.tests.factories import WorkflowFactory


class ValidationRunFilterTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.org = OrganizationFactory()
        self.user = UserFactory(orgs=[self.org])  # Fixed: was orgs=[self.org]

        self.project = ProjectFactory(org=self.org)
        self.workflow = WorkflowFactory(org=self.org, user=self.user)
        self.submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
        )

        # Mock get_current_org
        self.user.get_current_org = lambda: self.org

        self.client.force_authenticate(user=self.user)

    def test_filter_invalid_status(self):
        """Test filtering with invalid status returns 400 error."""
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"status": "invalid_status"})

        # django-filter with ChoiceFilter returns 400 for invalid choices
        self.assertEqual(response.status_code, 400)
        # Check that error message mentions the invalid choice
        self.assertIn("status", response.data)

    def test_filter_valid_status_no_matches(self):
        """Test filtering with valid status that has no matches returns empty results."""
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-list")
        # Use a valid status that doesn't match our created run
        response = self.client.get(url, {"status": ValidationRunStatus.SUCCEEDED})

        # Should return 200 but with empty results
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 0)

    def test_filter_nonexistent_workflow(self):
        """Test filtering with nonexistent workflow ID."""
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"workflow": 99999})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 0)

    def test_filter_date_formats(self):
        """Test that different date formats work correctly."""
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-list")
        today = timezone.now().date()

        # Test ISO date format
        response = self.client.get(url, {"on": today.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)

        # Test that after filter bypasses 30-day limit
        yesterday = (timezone.now() - timedelta(days=1)).date()
        response = self.client.get(url, {"after": yesterday.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)

    def test_combine_multiple_filters(self):
        """Test combining multiple filters."""
        # Create runs with different combinations
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(
            url, {"status": ValidationRunStatus.PENDING, "workflow": self.workflow.id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["status"], ValidationRunStatus.PENDING)

    def test_filter_invalid_date_format(self):
        """Test filtering with invalid date format returns 400."""
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"after": "not-a-date"})

        # Should return 400 for invalid date format
        self.assertEqual(response.status_code, 400)
        self.assertIn("after", response.data)
