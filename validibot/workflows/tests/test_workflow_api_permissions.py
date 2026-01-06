"""
Tests for workflow API permissions.

The WorkflowViewSet is read-only per ADR-2025-12-22 to minimize API attack
surface during the initial CLI rollout. Write operations (create, update,
delete) are only available through the web interface.

Updated for ADR-2026-01-06: Uses org-scoped API routes.

These tests verify that:
1. Read operations (list, retrieve) work correctly with proper permissions
2. Write operations return 405 Method Not Allowed for all users
"""

import json

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.tests.factories import WorkflowFactory


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def org(db):
    return OrganizationFactory()


@pytest.fixture
def manager(db, org):
    user = UserFactory()
    grant_role(user, org, RoleCode.ADMIN)
    user.set_current_org(org)
    return user


@pytest.fixture
def viewer(db, org):
    user = UserFactory()
    grant_role(user, org, RoleCode.WORKFLOW_VIEWER)
    user.set_current_org(org)
    return user


@pytest.fixture
def workflow(db, org):
    return WorkflowFactory(
        org=org,
        allowed_file_types=[SubmissionFileType.JSON],
    )


def test_create_not_allowed_for_any_user(api_client: APIClient, manager, org):
    """Create operations return 405 since the API is read-only."""
    api_client.force_authenticate(user=manager)
    payload = {
        "name": "API Workflow",
        "slug": "api-workflow",
        "allowed_file_types": [SubmissionFileType.JSON],
    }

    resp = api_client.post(
        f"/api/v1/orgs/{org.slug}/workflows/",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_update_not_allowed_for_any_user(api_client: APIClient, manager, workflow, org):
    """Update operations return 405 since the API is read-only."""
    api_client.force_authenticate(user=manager)

    resp = api_client.patch(
        f"/api/v1/orgs/{org.slug}/workflows/{workflow.pk}/",
        data=json.dumps({"name": "Renamed Workflow"}),
        content_type="application/json",
    )

    assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_delete_not_allowed_for_any_user(api_client: APIClient, manager, workflow, org):
    """Delete operations return 405 since the API is read-only."""
    api_client.force_authenticate(user=manager)

    resp = api_client.delete(f"/api/v1/orgs/{org.slug}/workflows/{workflow.pk}/")

    assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_viewer_can_list_workflows(api_client: APIClient, viewer, workflow, org):
    """Viewers can list workflows they have access to."""
    api_client.force_authenticate(user=viewer)

    resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")

    assert resp.status_code == status.HTTP_200_OK
    # Handle paginated response
    data = resp.data
    if isinstance(data, dict) and "results" in data:
        data = data["results"]
    assert len(data) >= 1


def test_viewer_can_retrieve_workflow(api_client: APIClient, viewer, workflow, org):
    """Viewers can retrieve individual workflow details."""
    api_client.force_authenticate(user=viewer)

    resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/{workflow.pk}/")

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["id"] == workflow.pk
