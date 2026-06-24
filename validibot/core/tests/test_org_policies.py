"""
Tests for the organization policy registry (core/policies.py).

Verifies:
- No policies registered → all actions allowed
- A denying policy blocks the action with a reason
- Multiple policies, first deny wins
- reset_org_policies() clears all policies
"""

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from validibot.core.policies import check_org_policies
from validibot.core.policies import register_org_policy
from validibot.core.policies import reset_org_policies


class OrgPolicyRegistryTests(SimpleTestCase):
    """Tests for register_org_policy / check_org_policies."""

    def setUp(self):
        """Reset policies before each test."""
        reset_org_policies()

    def tearDown(self):
        """Reset policies after each test to avoid leaking between tests."""
        reset_org_policies()

    def test_no_policies_allows_action(self):
        """With no policies registered, all actions should be allowed."""
        org = MagicMock()
        allowed, reason = check_org_policies(org, "launch_validation_run")

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_allowing_policy_allows_action(self):
        """A policy that returns allowed should let the action through."""

        def allow_policy(org, action, **context):
            return (True, "")

        register_org_policy(allow_policy)

        org = MagicMock()
        allowed, reason = check_org_policies(org, "launch_validation_run")

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_denying_policy_blocks_action(self):
        """A policy that denies should block the action with a reason."""

        def deny_policy(org, action, **context):
            return (False, "Trial expired")

        register_org_policy(deny_policy)

        org = MagicMock()
        allowed, reason = check_org_policies(org, "launch_validation_run")

        self.assertFalse(allowed)
        self.assertEqual(reason, "Trial expired")

    def test_first_deny_wins(self):
        """When multiple policies are registered, the first deny wins."""

        def allow_policy(org, action, **context):
            return (True, "")

        def deny_policy_1(org, action, **context):
            return (False, "First denial")

        def deny_policy_2(org, action, **context):
            return (False, "Second denial")

        register_org_policy(allow_policy)
        register_org_policy(deny_policy_1)
        register_org_policy(deny_policy_2)

        org = MagicMock()
        allowed, reason = check_org_policies(org, "launch_validation_run")

        self.assertFalse(allowed)
        self.assertEqual(reason, "First denial")

    def test_all_allowing_policies_pass(self):
        """When all policies allow, the action should be allowed."""

        def allow_1(org, action, **context):
            return (True, "")

        def allow_2(org, action, **context):
            return (True, "")

        register_org_policy(allow_1)
        register_org_policy(allow_2)

        org = MagicMock()
        allowed, reason = check_org_policies(org, "launch_validation_run")

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_reset_clears_all_policies(self):
        """reset_org_policies() should clear all registered policies."""

        def deny_policy(org, action, **context):
            return (False, "Denied")

        register_org_policy(deny_policy)
        reset_org_policies()

        org = MagicMock()
        allowed, reason = check_org_policies(org, "launch_validation_run")

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_superuser_bypasses_all_policies(self):
        """A superuser is an operator, not a tenant, and must bypass every
        registered policy.

        This matters because commercial packages register denying policies
        (trial expiry, quota, billing status). An operator acting through a
        superuser account should never be blocked by a tenant's commercial
        state — so the registry short-circuits before any policy runs, even
        a policy that would otherwise deny.
        """

        def deny_policy(org, action, **context):
            return (False, "Trial expired")

        register_org_policy(deny_policy)

        org = MagicMock()
        superuser = MagicMock(is_superuser=True)
        allowed, reason = check_org_policies(
            org,
            "launch_validation_run",
            user=superuser,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_non_superuser_does_not_bypass(self):
        """A non-superuser must still be subject to denying policies.

        Guards against the bypass being too broad: only ``is_superuser``
        waives policies. An ordinary authenticated user passing through the
        same ``user`` kwarg is still bound by the registered rules.
        """

        def deny_policy(org, action, **context):
            return (False, "Trial expired")

        register_org_policy(deny_policy)

        org = MagicMock()
        normal_user = MagicMock(is_superuser=False)
        allowed, reason = check_org_policies(
            org,
            "launch_validation_run",
            user=normal_user,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "Trial expired")

    def test_no_user_runs_policies_normally(self):
        """Omitting ``user`` (system/background action) runs policies as before.

        Background actions without an acting user must not accidentally gain
        the superuser bypass; absence of a user means "no operator override".
        """

        def deny_policy(org, action, **context):
            return (False, "Quota exceeded")

        register_org_policy(deny_policy)

        org = MagicMock()
        allowed, reason = check_org_policies(org, "launch_validation_run")

        self.assertFalse(allowed)
        self.assertEqual(reason, "Quota exceeded")

    def test_policy_receives_correct_arguments(self):
        """Policies should receive the org and action arguments."""
        received_args = {}

        def spy_policy(org, action, **context):
            received_args["org"] = org
            received_args["action"] = action
            received_args["context"] = context
            return (True, "")

        register_org_policy(spy_policy)

        mock_org = MagicMock()
        mock_org.name = "Test Org"
        check_org_policies(mock_org, "launch_validation_run")

        self.assertIs(received_args["org"], mock_org)
        self.assertEqual(received_args["action"], "launch_validation_run")
