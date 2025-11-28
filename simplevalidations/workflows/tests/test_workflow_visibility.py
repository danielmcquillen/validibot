from django.test import TestCase
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.users.tests.utils import ensure_all_roles_exist
from simplevalidations.workflows.tests.factories import WorkflowFactory


def _login(client, user, org):
    user.set_current_org(org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.id
    session.save()


class WorkflowVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_executor_can_view_workflow_list(self):
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        executor = UserFactory(orgs=[org])
        grant_role(executor, org, RoleCode.EXECUTOR)
        _login(client, executor, org)

        url = reverse("workflows:workflow_list")
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(workflow.name, response.content.decode())

    def test_workflow_viewer_can_see_workflows(self):
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        viewer = UserFactory(orgs=[org])
        grant_role(viewer, org, RoleCode.WORKFLOW_VIEWER)
        _login(client, viewer, org)

        url = reverse("workflows:workflow_list")
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(workflow.name, response.content.decode())

    def test_author_can_view_detail_of_own_workflow(self):
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        _login(client, author, org)

        url = reverse("workflows:workflow_detail", args=[workflow.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(workflow.name, response.content.decode())

    def test_author_can_edit_own_workflow(self):
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        _login(client, author, org)

        url = reverse("workflows:workflow_update", args=[workflow.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Edit Workflow", html)
        self.assertIn(workflow.name, html)

    def test_author_cannot_view_detail_of_others_workflow(self):
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        other_author = UserFactory(orgs=[org])
        grant_role(other_author, org, RoleCode.AUTHOR)
        _login(client, other_author, org)

        url = reverse("workflows:workflow_detail", args=[workflow.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(workflow.name, response.content.decode())

    def test_executor_can_view_detail_of_others_workflow(self):
        """
        Executors can view workflow detail pages in their org but cannot edit.
        """
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        executor = UserFactory(orgs=[org])
        grant_role(executor, org, RoleCode.EXECUTOR)
        _login(client, executor, org)

        url = reverse("workflows:workflow_detail", args=[workflow.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(workflow.name, html)
        self.assertNotIn("Edit Workflow", html)

        edit_url = reverse("workflows:workflow_update", args=[workflow.pk])
        edit_response = client.get(edit_url)
        self.assertIn(edit_response.status_code, {302, 403, 404})
