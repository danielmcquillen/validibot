"""
Tests for org-scoped API endpoints (ADR-2026-01-06).

Tests cover:
- Workflow identifier resolution (slug-first, then ID)
- Version selection (latest default, explicit version)
- Org scoping (membership required, 403 for non-members)
- Response format (url field present)
"""

import pytest
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from validibot.users.constants import RoleCode
from validibot.users.models import Role
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.tests.factories import WorkflowFactory


def _ensure_roles():
    """Ensure Role objects exist for all RoleCodes."""
    for code in RoleCode.values:
        Role.objects.get_or_create(
            code=code,
            defaults={
                "name": getattr(RoleCode, code).label
                if hasattr(RoleCode, code)
                else code.title(),
            },
        )


@pytest.mark.django_db(transaction=True)
class OrgScopedWorkflowAPITestCase(TransactionTestCase):
    """Tests for the org-scoped workflow API endpoints."""

    def setUp(self):
        _ensure_roles()
        self.client = APIClient()

        # Create test organization
        self.org = OrganizationFactory(slug="test-org")

        # Create test user with membership
        self.user = UserFactory(orgs=[self.org])
        grant_role(self.user, self.org, RoleCode.VALIDATION_RESULTS_VIEWER)

        # Create another org and user for isolation testing
        self.other_org = OrganizationFactory(slug="other-org")
        self.other_user = UserFactory(orgs=[self.other_org])
        grant_role(self.other_user, self.other_org, RoleCode.VALIDATION_RESULTS_VIEWER)

        # Create test workflows
        self.workflow = WorkflowFactory(
            org=self.org,
            user=self.user,
            slug="my-workflow",
            version="1",
        )

    def test_list_workflows_requires_authentication(self):
        """Unauthenticated requests should return 403."""
        url = reverse("api:org-workflows-list", kwargs={"org_slug": self.org.slug})
        response = self.client.get(url)
        # Returns 403 because OrgMembershipPermission denies anonymous users
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_workflows_requires_membership(self):
        """Non-members should receive 403."""
        self.client.force_authenticate(user=self.other_user)
        url = reverse("api:org-workflows-list", kwargs={"org_slug": self.org.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_workflows_success(self):
        """Members should see workflows in their org."""
        self.client.force_authenticate(user=self.user)
        url = reverse("api:org-workflows-list", kwargs={"org_slug": self.org.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Handle paginated response
        data = response.data
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["slug"], "my-workflow")

    def test_list_workflows_returns_latest_version_only(self):
        """List should return only the latest version of each workflow family."""
        # Create additional versions
        WorkflowFactory(
            org=self.org,
            user=self.user,
            slug="my-workflow",
            version="2",
        )
        WorkflowFactory(
            org=self.org,
            user=self.user,
            slug="my-workflow",
            version="3",
        )

        self.client.force_authenticate(user=self.user)
        url = reverse("api:org-workflows-list", kwargs={"org_slug": self.org.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Handle paginated response
        data = response.data
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        # Should only return 1 workflow (the latest version)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["version"], "3")

    def test_retrieve_workflow_by_slug(self):
        """Should retrieve workflow by slug."""
        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:org-workflows-detail",
            kwargs={"org_slug": self.org.slug, "pk": "my-workflow"},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["slug"], "my-workflow")

    def test_retrieve_workflow_by_id(self):
        """Should retrieve workflow by numeric ID."""
        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:org-workflows-detail",
            kwargs={"org_slug": self.org.slug, "pk": str(self.workflow.pk)},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], self.workflow.pk)

    def test_retrieve_workflow_returns_latest_version(self):
        """Slug lookup should return the latest version."""
        # Create a newer version
        WorkflowFactory(
            org=self.org,
            user=self.user,
            slug="my-workflow",
            version="2",
        )

        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:org-workflows-detail",
            kwargs={"org_slug": self.org.slug, "pk": "my-workflow"},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["version"], "2")

    def test_retrieve_nonexistent_workflow_404(self):
        """Should return 404 for nonexistent workflow."""
        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:org-workflows-detail",
            kwargs={"org_slug": self.org.slug, "pk": "nonexistent"},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_workflow_response_includes_url_field(self):
        """Workflow responses should include a url field."""
        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:org-workflows-detail",
            kwargs={"org_slug": self.org.slug, "pk": "my-workflow"},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("url", response.data)
        self.assertIn("/api/v1/orgs/test-org/workflows/", response.data["url"])

    def test_cross_org_workflow_not_accessible(self):
        """Users should not access workflows in orgs they're not members of."""
        # Create workflow in other org
        other_workflow = WorkflowFactory(
            org=self.other_org,
            user=self.other_user,
            slug="other-workflow",
        )

        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:org-workflows-detail",
            kwargs={"org_slug": self.other_org.slug, "pk": other_workflow.slug},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db(transaction=True)
class WorkflowVersionAPITestCase(TransactionTestCase):
    """Tests for the workflow version API endpoints."""

    def setUp(self):
        _ensure_roles()
        self.client = APIClient()
        self.org = OrganizationFactory(slug="test-org")
        self.user = UserFactory(orgs=[self.org])
        grant_role(self.user, self.org, RoleCode.VALIDATION_RESULTS_VIEWER)

        # Create multiple versions
        self.workflow_v1 = WorkflowFactory(
            org=self.org,
            user=self.user,
            slug="versioned-workflow",
            version="1",
        )
        self.workflow_v2 = WorkflowFactory(
            org=self.org,
            user=self.user,
            slug="versioned-workflow",
            version="2",
        )

    def test_list_versions(self):
        """Should list all versions of a workflow."""
        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:workflow-versions-list",
            kwargs={
                "org_slug": self.org.slug,
                "workflow_slug": "versioned-workflow",
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Handle paginated response
        data = response.data
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        self.assertEqual(len(data), 2)
        versions = {w["version"] for w in data}
        self.assertEqual(versions, {"1", "2"})

    def test_retrieve_specific_version(self):
        """Should retrieve a specific version."""
        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:workflow-versions-detail",
            kwargs={
                "org_slug": self.org.slug,
                "workflow_slug": "versioned-workflow",
                "version": "1",
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["version"], "1")

    def test_retrieve_nonexistent_version_404(self):
        """Should return 404 for nonexistent version."""
        self.client.force_authenticate(user=self.user)
        url = reverse(
            "api:workflow-versions-detail",
            kwargs={
                "org_slug": self.org.slug,
                "workflow_slug": "versioned-workflow",
                "version": "999",
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
