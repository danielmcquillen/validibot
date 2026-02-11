from django.test import TestCase

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import PermissionCode
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowAccessGrant


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
        grant_role(actor, org, RoleCode.VALIDATION_RESULTS_VIEWER)
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

    def test_guest_can_launch_workflow_with_access_grant(self):
        """Users with WorkflowAccessGrant can launch the workflow."""
        org = OrganizationFactory()
        owner = UserFactory()
        grant_role(owner, org, RoleCode.OWNER)

        workflow = Workflow.objects.create(
            org=org,
            user=owner,
            name="Shared Workflow",
        )

        # Guest user - not a member of the org
        guest = UserFactory()

        # Without grant, guest cannot launch
        self.assertFalse(
            guest.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow),
        )

        # Create an access grant for the guest
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            granted_by=owner,
            is_active=True,
        )

        # With grant, guest can launch
        self.assertTrue(
            guest.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow),
        )

    def test_guest_cannot_launch_with_inactive_grant(self):
        """Inactive WorkflowAccessGrant does not permit launch."""
        org = OrganizationFactory()
        owner = UserFactory()
        grant_role(owner, org, RoleCode.OWNER)

        workflow = Workflow.objects.create(
            org=org,
            user=owner,
            name="Shared Workflow",
        )

        guest = UserFactory()
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            granted_by=owner,
            is_active=False,  # Inactive
        )

        self.assertFalse(
            guest.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow),
        )

    def test_any_user_can_launch_public_workflow(self):
        """Public workflows can be launched by any authenticated user."""
        org = OrganizationFactory()
        owner = UserFactory()
        grant_role(owner, org, RoleCode.OWNER)

        workflow = Workflow.objects.create(
            org=org,
            user=owner,
            name="Public Workflow",
            is_public=True,
        )

        # Random user with no relationship to org
        random_user = UserFactory()

        self.assertTrue(
            random_user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow),
        )

    def test_non_public_workflow_requires_membership_or_grant(self):
        """Non-public workflows require org membership or explicit grant."""
        org = OrganizationFactory()
        owner = UserFactory()
        grant_role(owner, org, RoleCode.OWNER)

        workflow = Workflow.objects.create(
            org=org,
            user=owner,
            name="Private Workflow",
            is_public=False,
        )

        random_user = UserFactory()

        self.assertFalse(
            random_user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow),
        )
