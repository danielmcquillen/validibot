"""
Tests for billing metering and enforcement classes.

Tests BasicWorkflowMeter, AdvancedWorkflowMeter, and SeatEnforcer.
"""

import pytest
from django.test import TestCase
from django.utils import timezone

from validibot.billing.constants import PlanCode
from validibot.billing.constants import SubscriptionStatus
from validibot.billing.metering import AdvancedWorkflowMeter
from validibot.billing.metering import BasicWorkflowLimitError
from validibot.billing.metering import BasicWorkflowMeter
from validibot.billing.metering import InsufficientCreditsError
from validibot.billing.metering import SeatEnforcer
from validibot.billing.metering import SeatLimitError
from validibot.billing.metering import SubscriptionInactiveError
from validibot.billing.metering import TrialExpiredError
from validibot.billing.metering import get_or_create_monthly_counter
from validibot.billing.models import Plan
from validibot.billing.models import Subscription


class BasicWorkflowMeterTests(TestCase):
    """Tests for the BasicWorkflowMeter class."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        from validibot.users.models import Organization

        cls.plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 100,
                "included_credits": 50,
            },
        )
        cls.unlimited_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.ENTERPRISE,
            defaults={
                "name": "Enterprise",
                "basic_launches_limit": None,  # Unlimited
                "included_credits": 1000,
            },
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="test-org-basic-meter",
            defaults={"name": "Test Org Basic Meter"},
        )
        cls.meter = BasicWorkflowMeter()

    def test_check_and_increment_success_when_under_limit(self):
        """check_and_increment succeeds when under limit."""
        Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.ACTIVE,
        )

        # Should not raise
        self.meter.check_and_increment(self.org)

        # Verify counter was created and incremented
        counter = get_or_create_monthly_counter(self.org)
        self.assertEqual(counter.basic_launches, 1)

    def test_check_and_increment_raises_when_trial_expired(self):
        """check_and_increment raises TrialExpiredError for expired trials."""
        Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.TRIAL_EXPIRED,
        )

        with pytest.raises(TrialExpiredError):
            self.meter.check_and_increment(self.org)

    def test_check_and_increment_raises_when_subscription_inactive(self):
        """check_and_increment raises SubscriptionInactiveError for canceled subs."""
        Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.CANCELED,
        )

        with pytest.raises(SubscriptionInactiveError):
            self.meter.check_and_increment(self.org)

    def test_check_and_increment_raises_when_at_limit(self):
        """check_and_increment raises BasicWorkflowLimitError when at limit."""
        Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.ACTIVE,
        )

        # Create counter at limit
        counter = get_or_create_monthly_counter(self.org)
        counter.basic_launches = 100  # At limit
        counter.save()

        with pytest.raises(BasicWorkflowLimitError):
            self.meter.check_and_increment(self.org)

    def test_check_and_increment_unlimited_plan(self):
        """check_and_increment always succeeds for unlimited plans."""
        from validibot.users.models import Organization

        # Create new org for this test
        unlimited_org = Organization.objects.create(
            name="Unlimited Org",
            slug="unlimited-org",
        )
        Subscription.objects.create(
            org=unlimited_org,
            plan=self.unlimited_plan,
            status=SubscriptionStatus.ACTIVE,
        )

        # Create counter with high usage
        counter = get_or_create_monthly_counter(unlimited_org)
        counter.basic_launches = 10000
        counter.save()

        # Should not raise - unlimited
        self.meter.check_and_increment(unlimited_org)

    def test_get_usage_returns_counter_values(self):
        """get_usage returns current counter values."""
        Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.ACTIVE,
        )

        counter = get_or_create_monthly_counter(self.org)
        counter.basic_launches = 42
        counter.save()

        usage = self.meter.get_usage(self.org)
        self.assertEqual(usage["used"], 42)
        self.assertEqual(usage["limit"], 100)
        self.assertEqual(usage["remaining"], 58)
        self.assertFalse(usage["unlimited"])


class AdvancedWorkflowMeterTests(TestCase):
    """Tests for the AdvancedWorkflowMeter class."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        from validibot.users.models import Organization

        cls.plan, _ = Plan.objects.update_or_create(
            code=PlanCode.TEAM,
            defaults={
                "name": "Team",
                "basic_launches_limit": 500,
                "included_credits": 200,
            },
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="test-org-advanced-meter",
            defaults={"name": "Test Org Advanced Meter"},
        )
        cls.meter = AdvancedWorkflowMeter()

    def test_check_and_consume_success_with_included_credits(self):
        """check_can_launch + consume_credits deducts from included credits."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.ACTIVE,
            included_credits_remaining=100,
            purchased_credits_balance=0,
        )

        # Check first, then consume
        self.meter.check_can_launch(self.org, credits_required=10)
        self.meter.consume_credits(self.org, amount=10)

        subscription.refresh_from_db()
        self.assertEqual(subscription.included_credits_remaining, 90)
        self.assertEqual(subscription.purchased_credits_balance, 0)

    def test_consume_credits_uses_purchased_after_included(self):
        """consume_credits uses purchased credits after included are exhausted."""
        subscription = Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.ACTIVE,
            included_credits_remaining=5,
            purchased_credits_balance=20,
        )

        # Check first, then consume
        self.meter.check_can_launch(self.org, credits_required=10)
        self.meter.consume_credits(self.org, amount=10)

        subscription.refresh_from_db()
        self.assertEqual(subscription.included_credits_remaining, 0)
        self.assertEqual(subscription.purchased_credits_balance, 15)

    def test_check_can_launch_raises_when_insufficient_credits(self):
        """check_can_launch raises InsufficientCreditsError when not enough credits."""
        Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.ACTIVE,
            included_credits_remaining=5,
            purchased_credits_balance=0,
        )

        with pytest.raises(InsufficientCreditsError):
            self.meter.check_can_launch(self.org, credits_required=10)

    def test_check_can_launch_raises_when_trial_expired(self):
        """check_can_launch raises TrialExpiredError for expired trials."""
        Subscription.objects.create(
            org=self.org,
            plan=self.plan,
            status=SubscriptionStatus.TRIAL_EXPIRED,
            included_credits_remaining=100,
        )

        with pytest.raises(TrialExpiredError):
            self.meter.check_can_launch(self.org, credits_required=10)


class SeatEnforcerTests(TestCase):
    """Tests for the SeatEnforcer class."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        from validibot.users.models import Organization

        cls.plan_with_limit, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "max_seats": 3,
            },
        )
        cls.plan_unlimited, _ = Plan.objects.update_or_create(
            code=PlanCode.ENTERPRISE,
            defaults={
                "name": "Enterprise",
                "max_seats": None,
            },
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="test-org-seats-enforcer",
            defaults={"name": "Test Org Seats Enforcer"},
        )
        cls.enforcer = SeatEnforcer()

    def test_check_can_add_member_when_under_limit(self):
        """check_can_add_member succeeds when under limit."""
        Subscription.objects.create(
            org=self.org,
            plan=self.plan_with_limit,
            status=SubscriptionStatus.ACTIVE,
        )

        # Org has no members yet - should not raise
        self.enforcer.check_can_add_member(self.org)

    def test_check_can_add_member_raises_when_at_limit(self):
        """check_can_add_member raises SeatLimitError when at limit."""
        from validibot.users.models import Membership
        from validibot.users.models import User

        Subscription.objects.get_or_create(
            org=self.org,
            defaults={
                "plan": self.plan_with_limit,
                "status": SubscriptionStatus.ACTIVE,
            },
        )

        # Create 3 members (at limit)
        for i in range(3):
            user, _ = User.objects.get_or_create(
                email=f"seat_member{i}@example.com",
                defaults={
                    "username": f"seat_member{i}",
                    "password": "testpass123",  # noqa: S106
                },
            )
            Membership.objects.get_or_create(
                user=user,
                org=self.org,
                defaults={"is_active": True},
            )

        with pytest.raises(SeatLimitError):
            self.enforcer.check_can_add_member(self.org)

    def test_check_can_add_member_unlimited(self):
        """check_can_add_member succeeds for unlimited plans."""
        from validibot.users.models import Organization

        unlimited_org = Organization.objects.create(
            name="Unlimited Seats Org",
            slug="unlimited-seats",
        )
        Subscription.objects.create(
            org=unlimited_org,
            plan=self.plan_unlimited,
            status=SubscriptionStatus.ACTIVE,
        )

        # Should not raise - unlimited
        self.enforcer.check_can_add_member(unlimited_org)

    def test_get_seat_usage(self):
        """get_seat_usage returns correct usage stats."""
        from validibot.users.models import Membership
        from validibot.users.models import Organization
        from validibot.users.models import User

        # Use a fresh org specifically for this test to control member count
        test_org, _ = Organization.objects.get_or_create(
            slug="test-org-seat-usage",
            defaults={"name": "Test Org Seat Usage"},
        )

        Subscription.objects.get_or_create(
            org=test_org,
            defaults={
                "plan": self.plan_with_limit,
                "status": SubscriptionStatus.ACTIVE,
            },
        )

        # Create exactly 2 members
        for i in range(2):
            user, _ = User.objects.get_or_create(
                email=f"seat_usage_member{i}@example.com",
                defaults={
                    "username": f"seat_usage_member{i}",
                    "password": "testpass123",  # noqa: S106
                },
            )
            Membership.objects.get_or_create(
                user=user,
                org=test_org,
                defaults={"is_active": True},
            )

        usage = self.enforcer.get_seat_usage(test_org)
        self.assertEqual(usage["used"], 2)
        self.assertEqual(usage["limit"], 3)
        self.assertEqual(usage["remaining"], 1)
        self.assertFalse(usage["unlimited"])


class MonthlyCounterTests(TestCase):
    """Tests for monthly counter creation and management."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        from validibot.users.models import Organization

        cls.plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 100,
            },
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="counter-test-org",
            defaults={"name": "Counter Test Org"},
        )
        cls.subscription, _ = Subscription.objects.get_or_create(
            org=cls.org,
            defaults={
                "plan": cls.plan,
                "status": SubscriptionStatus.ACTIVE,
            },
        )

    def test_get_or_create_monthly_counter_creates_new(self):
        """get_or_create_monthly_counter creates counter if none exists."""
        counter = get_or_create_monthly_counter(self.org)

        self.assertIsNotNone(counter)
        self.assertEqual(counter.org, self.org)
        self.assertEqual(counter.basic_launches, 0)

    def test_get_or_create_monthly_counter_returns_existing(self):
        """get_or_create_monthly_counter returns existing counter for same period."""
        counter1 = get_or_create_monthly_counter(self.org)
        counter1.basic_launches = 42
        counter1.save()

        counter2 = get_or_create_monthly_counter(self.org)

        self.assertEqual(counter1.id, counter2.id)
        self.assertEqual(counter2.basic_launches, 42)

    def test_counter_period_boundaries(self):
        """Counter periods align to calendar months."""
        counter = get_or_create_monthly_counter(self.org)
        now = timezone.now().date()

        # Period start should be first of current month
        self.assertEqual(counter.period_start.day, 1)
        self.assertEqual(counter.period_start.month, now.month)
        self.assertEqual(counter.period_start.year, now.year)
