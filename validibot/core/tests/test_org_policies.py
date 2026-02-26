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
