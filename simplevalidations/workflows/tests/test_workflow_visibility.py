import pytest
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _set_org(client, user, org):
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()


def test_executor_cannot_view_workflow_list(client):
    org = OrganizationFactory()
    author = UserFactory(orgs=[org])
    grant_role(author, org, RoleCode.AUTHOR)
    workflow = WorkflowFactory(org=org, user=author)

    executor = UserFactory(orgs=[org])
    grant_role(executor, org, RoleCode.EXECUTOR)
    _set_org(client, executor, org)
    client.force_login(executor)

    url = reverse("workflows:workflow_list")
    response = client.get(url)
    assert response.status_code == 200
    assert workflow.name not in response.content.decode()


def test_workflow_viewer_can_see_workflows(client):
    org = OrganizationFactory()
    author = UserFactory(orgs=[org])
    grant_role(author, org, RoleCode.AUTHOR)
    workflow = WorkflowFactory(org=org, user=author)

    viewer = UserFactory(orgs=[org])
    grant_role(viewer, org, RoleCode.WORKFLOW_VIEWER)
    _set_org(client, viewer, org)
    client.force_login(viewer)

    url = reverse("workflows:workflow_list")
    response = client.get(url)
    assert response.status_code == 200
    assert workflow.name in response.content.decode()


def test_author_can_view_detail_of_own_workflow(client):
    org = OrganizationFactory()
    author = UserFactory(orgs=[org])
    grant_role(author, org, RoleCode.AUTHOR)
    workflow = WorkflowFactory(org=org, user=author)

    _set_org(client, author, org)
    client.force_login(author)

    url = reverse("workflows:workflow_detail", args=[workflow.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert workflow.name in response.content.decode()


def test_executor_cannot_view_detail_of_others_workflow(client):
    org = OrganizationFactory()
    author = UserFactory(orgs=[org])
    grant_role(author, org, RoleCode.AUTHOR)
    workflow = WorkflowFactory(org=org, user=author)

    executor = UserFactory(orgs=[org])
    grant_role(executor, org, RoleCode.EXECUTOR)
    _set_org(client, executor, org)
    client.force_login(executor)

    url = reverse("workflows:workflow_detail", args=[workflow.pk])
    response = client.get(url)
    assert response.status_code in {302, 404, 403}
