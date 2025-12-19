"""
Tests for billing models.

Tests Plan, Subscription, and related model functionality.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from validibot.billing.constants import TRIAL_DURATION_DAYS
from validibot.billing.constants import PlanCode
from validibot.billing.constants import SubscriptionStatus
from validibot.billing.models import Plan
from validibot.billing.models import Subscription


class PlanModelTests(TestCase):
    """Tests for the Plan model."""

    @classmethod
    def setUpTestData(cls):
        """Create test plans."""
        cls.starter_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 100,
                "included_credits": 50,
                "max_seats": 3,
                "max_workflows": 10,
                "monthly_price_cents": 2900,
                "display_order": 1,
                "has_integrations": False,
                "has_audit_logs": False,
            },
        )
        cls.team_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.TEAM,
            defaults={
                "name": "Team",
                "basic_launches_limit": 500,
                "included_credits": 200,
                "max_seats": 10,
                "max_workflows": 50,
                "has_integrations": True,
                "has_audit_logs": False,
                "monthly_price_cents": 9900,
                "display_order": 2,
            },
        )
        cls.enterprise_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.ENTERPRISE,
            defaults={
                "name": "Enterprise",
                "basic_launches_limit": None,  # Unlimited
                "included_credits": 1000,
                "max_seats": None,  # Unlimited
                "max_workflows": None,  # Unlimited
                "has_integrations": True,
                "has_audit_logs": True,
                "monthly_price_cents": 0,  # Contact sales
                "display_order": 3,
            },
        )

    def test_plan_str(self):
        """Plan string representation shows name."""
        self.assertEqual(str(self.starter_plan), "Starter")
        self.assertEqual(str(self.team_plan), "Team")
        self.assertEqual(str(self.enterprise_plan), "Enterprise")

    def test_plan_ordering(self):
        """Plans are ordered by display_order."""
        plans = list(Plan.objects.all())
        self.assertEqual(plans[0].code, PlanCode.STARTER)
        self.assertEqual(plans[1].code, PlanCode.TEAM)
        self.assertEqual(plans[2].code, PlanCode.ENTERPRISE)

    def test_plan_unlimited_fields(self):
        """Enterprise plan has unlimited (null) fields."""
        self.assertIsNone(self.enterprise_plan.basic_launches_limit)
        self.assertIsNone(self.enterprise_plan.max_seats)
        self.assertIsNone(self.enterprise_plan.max_workflows)

    def test_plan_feature_flags(self):
        """Feature flags are correctly set per plan."""
        self.assertFalse(self.starter_plan.has_integrations)
        self.assertFalse(self.starter_plan.has_audit_logs)
        self.assertTrue(self.team_plan.has_integrations)
        self.assertFalse(self.team_plan.has_audit_logs)
        self.assertTrue(self.enterprise_plan.has_integrations)
        self.assertTrue(self.enterprise_plan.has_audit_logs)


class SubscriptionModelTests(TestCase):
    """Tests for the Subscription model."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        from validibot.users.models import Organization
        from validibot.users.models import User

        cls.plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 100,
                "included_credits": 50,
                "max_seats": 3,
            },
        )

        # Create a user and organization
        cls.user, _ = User.objects.get_or_create(
            email="subscription_test@example.com",
            defaults={
                "username": "subscription_test",
                "password": "testpass123",
            },
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="subscription-model-test-org",
            defaults={"name": "Subscription Test Org"},
        )

    def test_subscription_creation(self):
        """Subscription can be created with default trial status."""
        now = timezone.now()
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=TRIAL_DURATION_DAYS),
        )

        self.assertEqual(subscription.status, SubscriptionStatus.TRIALING)
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(subscription.org, self.org)

    def test_subscription_str(self):
        """Subscription string shows org, plan, and status."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
        )
        expected = f"{self.org.name} - {self.plan.name} ({SubscriptionStatus.TRIALING})"
        self.assertEqual(str(subscription), expected)

    def test_total_credits_balance(self):
        """Total credits balance sums included and purchased credits."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            included_credits_remaining=30,
            purchased_credits_balance=20,
        )
        self.assertEqual(subscription.total_credits_balance, 50)

    def test_get_effective_limit_uses_plan_default(self):
        """get_effective_limit returns plan value when no custom override."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
        )
        self.assertEqual(
            subscription.get_effective_limit("basic_launches_limit"),
            100,
        )
        self.assertEqual(
            subscription.get_effective_limit("max_seats"),
            3,
        )

    def test_get_effective_limit_uses_custom_override(self):
        """get_effective_limit returns custom value when set."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            custom_basic_launches_limit=500,  # Enterprise override
            custom_max_seats=25,
        )
        self.assertEqual(
            subscription.get_effective_limit("basic_launches_limit"),
            500,
        )
        self.assertEqual(
            subscription.get_effective_limit("max_seats"),
            25,
        )

    def test_has_custom_limits(self):
        """has_custom_limits returns True when any override is set."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
        )
        self.assertFalse(subscription.has_custom_limits)

        subscription.custom_basic_launches_limit = 500
        self.assertTrue(subscription.has_custom_limits)


class SubscriptionStatusTransitionTests(TestCase):
    """Tests for subscription status transitions."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        from validibot.users.models import Organization

        cls.plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
            },
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="test-org-status-transitions",
            defaults={"name": "Status Test Org"},
        )

    def test_trial_to_active_transition(self):
        """Subscription can transition from TRIALING to ACTIVE."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.TRIALING,
        )

        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatus.ACTIVE)

    def test_trial_to_expired_transition(self):
        """Subscription can transition from TRIALING to TRIAL_EXPIRED."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.TRIALING,
        )

        subscription.status = SubscriptionStatus.TRIAL_EXPIRED
        subscription.save()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatus.TRIAL_EXPIRED)

    def test_active_to_past_due_transition(self):
        """Subscription can transition from ACTIVE to PAST_DUE."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.ACTIVE,
        )

        subscription.status = SubscriptionStatus.PAST_DUE
        subscription.save()

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatus.PAST_DUE)
