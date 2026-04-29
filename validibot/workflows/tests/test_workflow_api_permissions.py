"""
Tests for workflow API permissions.

The WorkflowViewSet is read-only per ADR-2025-12-22 to minimize API attack
surface during the initial CLI rollout. Write operations (create, update,
delete) are only available through the web interface.

Updated for ADR-2026-01-06: Uses org-scoped API routes.

These tests verify that:
1. Read operations (list, retrieve) work correctly with proper permissions
2. Write operations return 405 Method Not Allowed for all users
3. Guests with a workflow grant cannot enumerate other workflows in the same
   org (ADR-2026-04-27 ``[trust-#1]``).
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
from validibot.workflows.models import WorkflowAccessGrant
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


# ---------------------------------------------------------------------------
# Guest scoping regression tests for ADR-2026-04-27 [trust-#1].
#
# The bug: ``OrgMembershipPermission`` admitted any authenticated user with at
# least one active ``WorkflowAccessGrant`` in the org, and the viewset queryset
# then returned every non-archived workflow in the org. So a guest invited to
# workflow A could list and retrieve workflow B.
#
# The fix scopes the API queryset through ``Workflow.objects.for_user(...)``,
# which intersects org-membership, creator-of, and active grant access.
# ---------------------------------------------------------------------------


@pytest.fixture
def guest_with_grant(db, org, workflow):
    """Authenticated user with a grant for ``workflow`` only — no membership."""
    user = UserFactory(orgs=[])
    WorkflowAccessGrant.objects.create(
        workflow=workflow,
        user=user,
        granted_by=workflow.user,
        is_active=True,
    )
    return user


@pytest.fixture
def other_workflow(db, org):
    """A second workflow in the same org that ``guest_with_grant`` cannot see."""
    return WorkflowFactory(
        org=org,
        slug="other-workflow",
        allowed_file_types=[SubmissionFileType.JSON],
    )


def test_guest_list_returns_only_granted_workflow(
    api_client: APIClient, guest_with_grant, workflow, other_workflow, org
):
    """
    A guest must see only workflows they have an active grant for.

    Without the [trust-#1] fix the list returned every non-archived workflow in
    the org because ``OrgScopedWorkflowViewSet.get_queryset`` used a broad
    ``filter(org=...)`` rather than ``Workflow.objects.for_user(...)``. We
    assert here that the response contains the granted workflow but not the
    other one — so the regression would fail this test before the fix.
    """
    api_client.force_authenticate(user=guest_with_grant)

    resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")

    assert resp.status_code == status.HTTP_200_OK
    data = resp.data["results"] if isinstance(resp.data, dict) else resp.data
    returned_ids = {item["id"] for item in data}
    assert workflow.pk in returned_ids
    assert other_workflow.pk not in returned_ids


def test_guest_cannot_retrieve_other_workflow_by_slug(
    api_client: APIClient, guest_with_grant, other_workflow, org
):
    """
    Object-level access must be enforced on retrieve, not just list.

    Even if a user passes ``OrgMembershipPermission`` (because they have a
    grant somewhere in the org), the queryset must hide unrelated workflows
    so retrieve-by-slug 404s instead of leaking the workflow definition.
    """
    api_client.force_authenticate(user=guest_with_grant)

    resp = api_client.get(
        f"/api/v1/orgs/{org.slug}/workflows/{other_workflow.slug}/",
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_guest_cannot_retrieve_other_workflow_by_pk(
    api_client: APIClient, guest_with_grant, other_workflow, org
):
    """
    The numeric-pk fallback must use the same access-scoped queryset as slug
    lookup. Otherwise a guest could enumerate workflow ids and retrieve any
    workflow in the org by guessing primary keys.
    """
    api_client.force_authenticate(user=guest_with_grant)

    resp = api_client.get(
        f"/api/v1/orgs/{org.slug}/workflows/{other_workflow.pk}/",
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_guest_can_retrieve_granted_workflow(
    api_client: APIClient, guest_with_grant, workflow, org
):
    """
    Sanity check the positive case: the access-scoped queryset must still let
    a guest read the workflow they were granted access to. This protects us
    from over-tightening the queryset and breaking guest access entirely.
    """
    api_client.force_authenticate(user=guest_with_grant)

    resp = api_client.get(
        f"/api/v1/orgs/{org.slug}/workflows/{workflow.slug}/",
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["id"] == workflow.pk


def test_guest_cannot_list_versions_of_other_workflow(
    api_client: APIClient, guest_with_grant, other_workflow, org
):
    """
    The version-pinned API surface (``/workflows/<slug>/versions/``) must
    apply the same object-level scoping as the latest-version surface. A guest
    who only has a grant for workflow A must not be able to enumerate or
    retrieve any version of workflow B.
    """
    api_client.force_authenticate(user=guest_with_grant)

    resp = api_client.get(
        f"/api/v1/orgs/{org.slug}/workflows/{other_workflow.slug}/versions/",
    )

    # Versions endpoints are still rooted under the org — empty list is fine,
    # but no row from ``other_workflow`` may appear.
    assert resp.status_code == status.HTTP_200_OK
    data = resp.data["results"] if isinstance(resp.data, dict) else resp.data
    returned_ids = {item["id"] for item in data}
    assert other_workflow.pk not in returned_ids


def test_org_member_still_sees_all_workflows_in_org(
    api_client: APIClient, viewer, workflow, other_workflow, org
):
    """
    The fix must not break org members. A workflow viewer with a role in the
    org should still see every non-archived workflow returned by the list
    endpoint — that is the difference between an org role and a guest grant.
    """
    api_client.force_authenticate(user=viewer)

    resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")

    assert resp.status_code == status.HTTP_200_OK
    data = resp.data["results"] if isinstance(resp.data, dict) else resp.data
    returned_ids = {item["id"] for item in data}
    assert workflow.pk in returned_ids
    assert other_workflow.pk in returned_ids


def test_superuser_still_sees_all_workflows_in_org_without_membership(
    api_client: APIClient, workflow, other_workflow, org
):
    """
    Superusers keep the intentional org-wide debug view without membership.

    The guest-scoping fix routes normal callers through
    ``Workflow.objects.for_user(...)``. Superusers have an explicit carve-out
    in ``OrgMembershipPermission`` and ``OrgScopedWorkflowViewSet`` so support
    staff can inspect an org even when they are not an org member or guest.
    """
    superuser = UserFactory(is_superuser=True, is_staff=True, orgs=[])
    api_client.force_authenticate(user=superuser)

    resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")

    assert resp.status_code == status.HTTP_200_OK
    data = resp.data["results"] if isinstance(resp.data, dict) else resp.data
    returned_ids = {item["id"] for item in data}
    assert workflow.pk in returned_ids
    assert other_workflow.pk in returned_ids
