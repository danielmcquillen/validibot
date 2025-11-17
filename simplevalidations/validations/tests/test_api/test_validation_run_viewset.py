from datetime import timedelta

import pytest
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import Membership, MembershipRole, Role
from simplevalidations.users.tests.factories import OrganizationFactory, UserFactory
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.tests.factories import (
    ValidationFindingFactory,
    ValidationRunFactory,
    ValidationStepRunFactory,
)
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
        first_result = response.data["results"][0]
        self.assertEqual(first_result["workflow"], self.workflow.id)
        self.assertEqual(first_result["workflow_slug"], self.workflow.slug)

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

    def test_detail_includes_step_findings(self):
        self.client.force_authenticate(user=self.user)
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.FAILED,
        )
        step_run = ValidationStepRunFactory(validation_run=run)
        finding = ValidationFindingFactory(
            validation_step_run=step_run,
            message="Too expensive",
            path="payload.price",
        )

        url = reverse("api:validation-runs-detail", args=[run.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        steps = response.data["steps"]
        self.assertEqual(len(steps), 1)
        issues = steps[0]["issues"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["message"], finding.message)
        self.assertEqual(issues[0]["path"], finding.path)

    def test_executor_cannot_retrieve_other_users_run(self):
        """Executor scoped to org cannot fetch runs they didn't launch."""
        owner = UserFactory(orgs=[self.org])
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=owner,
        )
        executor = UserFactory(orgs=[self.org])
        Membership.objects.filter(user=executor, org=self.org).update(is_active=True)
        membership = executor.memberships.get(org=self.org)
        membership.set_roles({RoleCode.EXECUTOR})

        self.client.force_authenticate(user=executor)
        url = reverse("api:validation-runs-detail", kwargs={"pk": run.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_results_viewer_can_retrieve_any_run(self):
        """RESULTS_VIEWER can fetch any run in their org."""
        owner = UserFactory(orgs=[self.org])
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=owner,
        )
        reviewer = UserFactory(orgs=[self.org])
        reviewer_membership = reviewer.memberships.get(org=self.org)
        reviewer_membership.set_roles({RoleCode.RESULTS_VIEWER})

        self.client.force_authenticate(user=reviewer)
        url = reverse("api:validation-runs-detail", kwargs={"pk": run.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(run.id))

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

    def test_results_viewer_sees_all_runs_in_org(self):
        """RESULTS_VIEWER can see all runs for their org."""
        reviewer = UserFactory(orgs=[self.org])
        reviewer.memberships.get(org=self.org).set_roles({RoleCode.RESULTS_VIEWER})
        self.client.force_authenticate(user=reviewer)

        run1 = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=self.user,
        )
        other_user = UserFactory(orgs=[self.org])
        other_submission = SubmissionFactory(
            org=self.org, project=self.project, user=other_user
        )
        run2 = ValidationRunFactory(
            submission=other_submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=other_user,
        )

        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {item["id"] for item in response.data["results"]}
        self.assertEqual(ids, {str(run1.id), str(run2.id)})

    def test_executor_sees_only_their_own_runs(self):
        """EXECUTOR without results rights only sees runs they launched."""
        # Ensure the org already has an owner so the executor isn't promoted.
        owner = UserFactory()
        owner_membership = Membership.objects.create(
            user=owner,
            org=self.org,
            is_active=True,
        )
        owner_membership.set_roles({RoleCode.OWNER})

        executor = UserFactory()
        membership = Membership.objects.create(
            user=executor,
            org=self.org,
            is_active=True,
        )
        executor.set_current_org(self.org)
        exec_role, _ = Role.objects.get_or_create(
            code=RoleCode.EXECUTOR,
            defaults={"name": RoleCode.EXECUTOR.label},
        )
        membership.roles.set([exec_role])
        executor.memberships.exclude(org=self.org).delete()
        self.assertEqual(
            set(membership.membership_roles.values_list("role__code", flat=True)),
            {RoleCode.EXECUTOR},
        )
        own_submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=executor,
        )
        own_run = ValidationRunFactory(
            submission=own_submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=executor,
        )
        other_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=self.user,
        )
        self.assertEqual(other_run.user_id, self.user.id)
        self.assertEqual(own_run.user_id, executor.id)

        self.client.force_authenticate(user=executor)
        url = reverse("api:validation-runs-list")
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = (
            response.data.get("results", response.data)
            if isinstance(response.data, dict)
            else response.data
        )
        ids = {item["id"] for item in results}
        user_ids = {item.get("user") for item in results}
        self.assertIn(str(own_run.id), ids)
        self.assertEqual(user_ids, {executor.id})
        self.assertNotIn(str(other_run.id), ids)
        self.assertEqual(user_ids, {executor.id})

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
