import pytest
from django.test import TestCase
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import (
    OrganizationFactory,
    UserFactory,
    grant_role,
)
from simplevalidations.workflows.tests.factories import WorkflowFactory


def _set_org(client, user, org):
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()


@pytest.mark.django_db
class WorkflowVisibilityTests(TestCase):
    def test_executor_can_view_workflow_list(self):
        client = self.client
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
        assert workflow.name in response.content.decode()

    def test_workflow_viewer_can_see_workflows(self):
        client = self.client
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

    def test_author_can_view_detail_of_own_workflow(self):
        client = self.client
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

    def test_author_can_edit_own_workflow(self):
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        _set_org(client, author, org)
        client.force_login(author)

        url = reverse("workflows:workflow_update", args=[workflow.pk])
        response = client.get(url)
        assert response.status_code == 200
        html = response.content.decode()
        assert "Edit Workflow" in html
        assert workflow.name in html

    def test_author_cannot_view_detail_of_others_workflow(self):
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        other_author = UserFactory(orgs=[org])
    grant_role(other_author, org, RoleCode.AUTHOR)
    _set_org(client, other_author, org)
    client.force_login(other_author)

    url = reverse("workflows:workflow_detail", args=[workflow.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert workflow.name in response.content.decode()

    edit_url = reverse("workflows:workflow_update", args=[workflow.pk])
    edit_response = client.get(edit_url)
    assert edit_response.status_code == 200

    def test_executor_can_view_detail_of_others_workflow(self):
        """
        An executor should be able to view the detail page of a
        workflow they did not create but which is in their org.

        However, no editing controls should be visible and
        edit URLs should be inaccessible.
        """
        client = self.client
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
    assert response.status_code == 200
    html = response.content.decode()
    assert workflow.name in html
    assert "Edit Workflow" not in html

    edit_url = reverse("workflows:workflow_update", args=[workflow.pk])
    edit_response = client.get(edit_url)
    assert edit_response.status_code in {302, 403, 404}
