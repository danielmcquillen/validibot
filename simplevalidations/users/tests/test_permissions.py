from django.test import TestCase

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.constants import PermissionCode, RoleCode
from simplevalidations.users.models import Membership
from simplevalidations.users.tests.factories import (
    OrganizationFactory,
    UserFactory,
    grant_role,
)
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.workflows.models import Workflow


class OrgPermissionBackendTests(TestCase):
    """
    Validate that the org-scoped permission backend honors role-to-permission
    mappings and supports object-aware permission checks.
    """

    def test_executor_can_launch_workflow(self):
        org = OrganizationFactory()
        executor = UserFactory()
        grant_role(executor, org, RoleCode.EXECUTOR)
        workflow = Workflow.objects.create(
            org=org,
            user=executor,
            name="Launchable Workflow",
        )

        self.assertTrue(
            executor.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow),
        )

        outsider = UserFactory()
        self.assertFalse(
            outsider.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow),
        )

    def test_results_viewer_can_see_all_runs(self):
        org = OrganizationFactory()
        actor = UserFactory()
        grant_role(actor, org, RoleCode.RESULTS_VIEWER)
        submission = SubmissionFactory(org=org, user=actor)
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=submission.user,
        )

        self.assertTrue(
            actor.has_perm(PermissionCode.VALIDATION_RESULTS_VIEW_ALL.value, run),
        )

    def test_executor_only_sees_own_runs(self):
        org = OrganizationFactory()
        executor = UserFactory()
        grant_role(executor, org, RoleCode.EXECUTOR)
        submission = SubmissionFactory(org=org, user=executor)
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=executor,
        )

        self.assertTrue(
            executor.has_perm(PermissionCode.VALIDATION_RESULTS_VIEW_OWN.value, run),
        )

        reviewer = UserFactory()
        grant_role(reviewer, org, RoleCode.WORKFLOW_VIEWER)
        self.assertFalse(
            reviewer.has_perm(PermissionCode.VALIDATION_RESULTS_VIEW_OWN.value, run),
        )

    def test_admin_manage_org_permission(self):
        org = OrganizationFactory()
        admin = UserFactory()
        grant_role(admin, org, RoleCode.ADMIN)

        self.assertTrue(
            admin.has_perm(PermissionCode.ADMIN_MANAGE_ORG.value, org),
        )

        member = UserFactory()
        grant_role(member, org, RoleCode.WORKFLOW_VIEWER)
        Membership.objects.filter(user=member, org=org).update(is_active=True)

        self.assertFalse(
            member.has_perm(PermissionCode.ADMIN_MANAGE_ORG.value, org),
        )
