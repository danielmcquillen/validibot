from django.test import TestCase
from django.urls import reverse

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.tests.factories import ValidationRunFactory


class ValidationRunListViewTests(TestCase):
    def test_owner_can_delete_validation_runs(self):
        org = OrganizationFactory()
        owner = UserFactory(orgs=[org])
        grant_role(owner, org, RoleCode.OWNER)
        owner.memberships.get(org=org).set_roles({RoleCode.OWNER})
        owner.set_current_org(org)

        submission = SubmissionFactory(
            org=org,
            user=owner,
            project__org=org,
        )
        owner.memberships.get(org=org).set_roles({RoleCode.OWNER})
        ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=owner,
        )

        self.client.force_login(owner)
        response = self.client.get(reverse("validations:validation_list"))

        self.assertEqual(response.status_code, 200)
        validations = list(response.context["validations"])
        self.assertTrue(validations)
        self.assertTrue(validations[0].curr_user_can_delete)

    def test_results_viewer_can_see_all_runs(self):
        org = OrganizationFactory()
        owner = UserFactory(orgs=[org])
        grant_role(owner, org, RoleCode.OWNER)
        owner.set_current_org(org)

        submission = SubmissionFactory(
            org=org,
            user=owner,
            project__org=org,
        )
        run = ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=owner,
        )

        reviewer = UserFactory(orgs=[org])
        grant_role(reviewer, org, RoleCode.RESULTS_VIEWER)
        reviewer.memberships.get(org=org).set_roles({RoleCode.RESULTS_VIEWER})
        reviewer.set_current_org(org)

        self.client.force_login(reviewer)
        response = self.client.get(reverse("validations:validation_list"))

        self.assertEqual(response.status_code, 200)
        validations = list(response.context["validations"])
        self.assertEqual(len(validations), 1)
        self.assertEqual(validations[0].pk, run.pk)
        self.assertTrue(validations[0].curr_user_can_view)
        self.assertFalse(validations[0].curr_user_can_delete)

    def test_executor_cannot_delete_validation_runs(self):
        org = OrganizationFactory()
        executor = UserFactory(orgs=[org])
        grant_role(executor, org, RoleCode.EXECUTOR)
        executor.memberships.get(org=org).set_roles({RoleCode.EXECUTOR})
        executor.set_current_org(org)

        submission = SubmissionFactory(
            org=org,
            user=executor,
            project__org=org,
        )
        executor.memberships.get(org=org).set_roles({RoleCode.EXECUTOR})
        ValidationRunFactory(
            submission=submission,
            org=org,
            workflow=submission.workflow,
            project=submission.project,
            user=executor,
        )
        executor.memberships.get(org=org).set_roles({RoleCode.EXECUTOR})
        # Another user's run should not appear for executor
        other_user = UserFactory(orgs=[org])
        grant_role(other_user, org, RoleCode.AUTHOR)
        other_user.set_current_org(org)
        other_submission = SubmissionFactory(
            org=org,
            user=other_user,
            project__org=org,
        )
        ValidationRunFactory(
            submission=other_submission,
            org=org,
            workflow=other_submission.workflow,
            project=other_submission.project,
            user=other_user,
        )

        self.client.force_login(executor)
        response = self.client.get(reverse("validations:validation_list"))

        self.assertEqual(response.status_code, 200)
        membership = executor.memberships.get(org=org)
        self.assertEqual(membership.role_codes, {RoleCode.EXECUTOR})
        validations = list(response.context["validations"])
        self.assertEqual(len(validations), 1)
        self.assertFalse(validations[0].curr_user_can_delete)
