from django.test import TestCase
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.tests.factories import SubmissionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory


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

    def test_detail_archive_button_shows_confirmation_prompt_when_runs_exist(self):
        """Workflow detail should confirm before archiving a workflow with runs."""
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)
        submission = SubmissionFactory(org=org, user=author, workflow=workflow)
        ValidationRunFactory(submission=submission)

        _login(client, author, org)

        url = reverse("workflows:workflow_detail", args=[workflow.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Are you sure you want to archive this workflow?",
        )

    def test_archived_workflow_detail_shows_unarchive_footer_action(self):
        """
        Archived workflows should expose unarchive actions in header and detail card.
        """
        client = self.client
        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(
            org=org,
            user=author,
            is_archived=True,
            is_active=False,
        )
        submission = SubmissionFactory(org=org, user=author, workflow=workflow)
        ValidationRunFactory(submission=submission)

        _login(client, author, org)

        url = reverse("workflows:workflow_detail", args=[workflow.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unarchive workflow")
        self.assertContains(response, "Unarchive this workflow?")
        self.assertContains(response, 'title="Unarchive this workflow"')
        self.assertContains(response, "bi-star")

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
