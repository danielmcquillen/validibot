import json

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.tests.factories import WorkflowFactory


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
    grant_role(user, org, RoleCode.VIEWER)
    user.set_current_org(org)
    return user


@pytest.fixture
def workflow(db, org):
    return WorkflowFactory(
        org=org,
        allowed_file_types=[SubmissionFileType.JSON],
    )


def test_viewer_cannot_create_workflow(api_client: APIClient, viewer, org):
    api_client.force_authenticate(user=viewer)
    payload = {
        "name": "Viewer Attempt",
        "slug": "viewer-attempt",
        "allowed_file_types": [SubmissionFileType.JSON],
        "org": org.id,
    }
    resp = api_client.post(
        "/api/v1/workflows/",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_manager_creates_workflow_scoped_to_org(api_client: APIClient, manager, org):
    api_client.force_authenticate(user=manager)
    payload = {
        "name": "API Workflow",
        "slug": "api-workflow",
        "allowed_file_types": [SubmissionFileType.JSON],
    }

    resp = api_client.post(
        "/api/v1/workflows/",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert resp.status_code == status.HTTP_201_CREATED, resp.content
    workflow_id = resp.data["id"]
    workflow = Workflow.objects.get(pk=workflow_id)
    assert workflow.org_id == org.id
    assert workflow.user_id == manager.id


def test_manager_cannot_create_in_unjoined_org(api_client: APIClient, manager):
    other_org = OrganizationFactory()
    api_client.force_authenticate(user=manager)

    resp = api_client.post(
        "/api/v1/workflows/",
        data=json.dumps(
            {
                "name": "Bad Org",
                "slug": "bad-org",
                "org": other_org.id,
            },
        ),
        content_type="application/json",
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_viewer_cannot_update_or_delete(api_client: APIClient, viewer, workflow):
    api_client.force_authenticate(user=viewer)
    url = f"/api/v1/workflows/{workflow.pk}/"

    resp = api_client.patch(
        url,
        data=json.dumps({"name": "Blocked"}),
        content_type="application/json",
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN

    resp = api_client.delete(url)
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_manager_update_keeps_org_fixed(api_client: APIClient, manager, workflow, org):
    api_client.force_authenticate(user=manager)
    other_org = OrganizationFactory()

    resp = api_client.patch(
        f"/api/v1/workflows/{workflow.pk}/",
        data=json.dumps(
            {
                "name": "Renamed Workflow",
                "org": other_org.id,
            },
        ),
        content_type="application/json",
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    workflow.refresh_from_db()
    assert workflow.name != "Renamed Workflow"
    assert workflow.org_id == org.id


def test_manager_can_delete(api_client: APIClient, manager, workflow):
    api_client.force_authenticate(user=manager)
    resp = api_client.delete(f"/api/v1/workflows/{workflow.pk}/")

    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert not Workflow.objects.filter(pk=workflow.pk).exists()
