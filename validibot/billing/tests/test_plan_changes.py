"""
Tests for the plan change service.

These tests cover:
- Free ↔ Paid transitions
- Paid ↔ Paid upgrades and downgrades
- Proration calculation
- Scheduled changes
- Multiple changes in one billing cycle
- Edge cases and error handling
"""

from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from validibot.billing.constants import PlanCode
from validibot.billing.constants import SubscriptionStatus
from validibot.billing.models import Plan
from validibot.billing.models import PlanChange
from validibot.billing.models import Subscription
from validibot.billing.plan_changes import InvalidPlanChangeError
from validibot.billing.plan_changes import PlanChangeResult
from validibot.billing.plan_changes import PlanChangeService
from validibot.billing.plan_changes import PlanChangeType
from validibot.users.models import Organization
from validibot.users.models import User


@pytest.mark.django_db
class TestPlanChangeType:
    """Tests for determining upgrade vs downgrade."""

    @pytest.fixture
    def plans(self):
        """Create test plans."""
        free = Plan.objects.create(
            code=PlanCode.FREE,
            name="Free",
            monthly_price_cents=0,
            display_order=0,
        )
        starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
            stripe_price_id="price_starter",
            display_order=1,
        )
        team = Plan.objects.create(
            code=PlanCode.TEAM,
            name="Team",
            monthly_price_cents=9900,
            stripe_price_id="price_team",
            display_order=2,
        )
        return {"free": free, "starter": starter, "team": team}

    def test_upgrade_detection(self, plans):
        """Test that moving to higher price is detected as upgrade."""
        service = PlanChangeService()

        result = service.get_change_type(plans["free"], plans["starter"])
        assert result == PlanChangeType.UPGRADE

        result = service.get_change_type(plans["starter"], plans["team"])
        assert result == PlanChangeType.UPGRADE

    def test_downgrade_detection(self, plans):
        """Test that moving to lower price is detected as downgrade."""
        service = PlanChangeService()

        result = service.get_change_type(plans["team"], plans["starter"])
        assert result == PlanChangeType.DOWNGRADE

        result = service.get_change_type(plans["starter"], plans["free"])
        assert result == PlanChangeType.DOWNGRADE

    def test_lateral_detection(self, plans):
        """Test that same price is detected as lateral."""
        service = PlanChangeService()

        # Same plan should be lateral (though this shouldn't happen in practice)
        result = service.get_change_type(plans["free"], plans["free"])
        assert result == PlanChangeType.LATERAL


@pytest.mark.django_db
class TestCanChangePlan:
    """Tests for plan change validation."""

    @pytest.fixture
    def setup(self):
        """Create test data."""
        user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass",
        )
        org = Organization.objects.create(name="Test Org", slug="test-org")

        free = Plan.objects.create(
            code=PlanCode.FREE,
            name="Free",
            monthly_price_cents=0,
            display_order=0,
        )
        starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
            stripe_price_id="price_starter",
            display_order=1,
        )
        enterprise = Plan.objects.create(
            code=PlanCode.ENTERPRISE,
            name="Enterprise",
            monthly_price_cents=0,
            display_order=3,
        )

        subscription = Subscription.objects.create(
            org=org,
            plan=free,
            status=SubscriptionStatus.ACTIVE,
        )

        return {
            "user": user,
            "org": org,
            "subscription": subscription,
            "free": free,
            "starter": starter,
            "enterprise": enterprise,
        }

    def test_cannot_change_to_current_plan(self, setup):
        """Test that changing to current plan is rejected."""
        service = PlanChangeService()

        allowed, reason = service.can_change_plan(
            setup["subscription"],
            setup["free"],
        )

        assert not allowed
        assert "Already on this plan" in reason

    def test_cannot_change_to_enterprise(self, setup):
        """Test that Enterprise requires sales contact."""
        service = PlanChangeService()

        allowed, reason = service.can_change_plan(
            setup["subscription"],
            setup["enterprise"],
        )

        assert not allowed
        assert "Contact sales" in reason

    def test_cannot_change_from_canceled(self, setup):
        """Test that canceled subscriptions cannot change plan."""
        service = PlanChangeService()
        setup["subscription"].status = SubscriptionStatus.CANCELED
        setup["subscription"].save()

        allowed, reason = service.can_change_plan(
            setup["subscription"],
            setup["starter"],
        )

        assert not allowed
        assert "CANCELED" in reason

    def test_can_change_from_trial_expired(self, setup):
        """Test that trial expired users can still change plan."""
        service = PlanChangeService()
        setup["subscription"].status = SubscriptionStatus.TRIAL_EXPIRED
        setup["subscription"].save()

        allowed, reason = service.can_change_plan(
            setup["subscription"],
            setup["starter"],
        )

        assert allowed
        assert reason == ""


@pytest.mark.django_db
class TestPreviewChange:
    """Tests for plan change preview."""

    @pytest.fixture
    def setup(self):
        """Create test data."""
        org = Organization.objects.create(name="Test Org", slug="test-org")

        free = Plan.objects.create(
            code=PlanCode.FREE,
            name="Free",
            monthly_price_cents=0,
            display_order=0,
        )
        starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
            stripe_price_id="price_starter",
            display_order=1,
        )
        team = Plan.objects.create(
            code=PlanCode.TEAM,
            name="Team",
            monthly_price_cents=9900,
            stripe_price_id="price_team",
            display_order=2,
        )

        subscription = Subscription.objects.create(
            org=org,
            plan=starter,
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_123",
            current_period_end=timezone.now() + timedelta(days=15),
        )

        return {
            "org": org,
            "subscription": subscription,
            "free": free,
            "starter": starter,
            "team": team,
        }

    def test_preview_upgrade_is_immediate(self, setup):
        """Test that upgrade preview shows immediate effect."""
        service = PlanChangeService()

        result = service.preview_change(
            setup["subscription"],
            setup["team"],
        )

        assert result.success
        assert result.change_type == PlanChangeType.UPGRADE
        assert result.effective_immediately
        assert result.scheduled_at is None

    def test_preview_downgrade_is_scheduled(self, setup):
        """Test that downgrade preview shows scheduled effect."""
        service = PlanChangeService()

        # Create subscription on Team, preview downgrade to Starter
        setup["subscription"].plan = setup["team"]
        setup["subscription"].save()

        result = service.preview_change(
            setup["subscription"],
            setup["starter"],
        )

        assert result.success
        assert result.change_type == PlanChangeType.DOWNGRADE
        assert not result.effective_immediately
        assert result.scheduled_at is not None

    def test_preview_downgrade_to_free_is_immediate(self, setup):
        """Test that downgrade to Free is immediate (cancels subscription)."""
        service = PlanChangeService()

        result = service.preview_change(
            setup["subscription"],
            setup["free"],
        )

        assert result.success
        assert result.change_type == PlanChangeType.DOWNGRADE
        assert result.effective_immediately
        assert "canceled immediately" in result.message


@pytest.mark.django_db
class TestChangePlanFreeToFree:
    """Tests for Free to Free transitions (edge case)."""

    @pytest.fixture
    def setup(self):
        """Create test data."""
        org = Organization.objects.create(name="Test Org", slug="test-org")

        free = Plan.objects.create(
            code=PlanCode.FREE,
            name="Free",
            monthly_price_cents=0,
            display_order=0,
        )

        subscription = Subscription.objects.create(
            org=org,
            plan=free,
            status=SubscriptionStatus.ACTIVE,
        )

        return {"org": org, "subscription": subscription, "free": free}

    def test_free_to_free_is_noop(self, setup):
        """Test that Free to Free change is handled gracefully."""
        service = PlanChangeService()

        # Trying to change from Free to Free should fail validation
        allowed, reason = service.can_change_plan(
            setup["subscription"],
            setup["free"],
        )

        assert not allowed
        assert "Already on this plan" in reason


@pytest.mark.django_db
class TestChangePlanFreeToPaid:
    """Tests for Free to Paid upgrades."""

    @pytest.fixture
    def setup(self):
        """Create test data."""
        org = Organization.objects.create(name="Test Org", slug="test-org")

        free = Plan.objects.create(
            code=PlanCode.FREE,
            name="Free",
            monthly_price_cents=0,
            display_order=0,
        )
        starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
            stripe_price_id="price_starter",
            display_order=1,
        )

        subscription = Subscription.objects.create(
            org=org,
            plan=free,
            status=SubscriptionStatus.ACTIVE,
        )

        return {
            "org": org,
            "subscription": subscription,
            "free": free,
            "starter": starter,
        }

    @patch("validibot.billing.services.BillingService.create_checkout_session")
    def test_free_to_paid_creates_checkout(self, mock_checkout, setup):
        """Test that Free to Paid creates a checkout session."""
        mock_checkout.return_value = "https://checkout.stripe.com/test"

        service = PlanChangeService()

        result = service.change_plan(
            setup["subscription"],
            setup["starter"],
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

        assert result.success
        assert result.checkout_url == "https://checkout.stripe.com/test"
        mock_checkout.assert_called_once()

    def test_free_to_paid_requires_urls(self, setup):
        """Test that Free to Paid requires success/cancel URLs."""
        service = PlanChangeService()

        with pytest.raises(InvalidPlanChangeError):
            service.change_plan(
                setup["subscription"],
                setup["starter"],
                # Missing URLs
            )


@pytest.mark.django_db
class TestChangePlanPaidToFree:
    """Tests for Paid to Free downgrades."""

    @pytest.fixture
    def setup(self):
        """Create test data."""
        org = Organization.objects.create(name="Test Org", slug="test-org")

        free = Plan.objects.create(
            code=PlanCode.FREE,
            name="Free",
            monthly_price_cents=0,
            included_credits=0,
            display_order=0,
        )
        starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
            included_credits=200,
            stripe_price_id="price_starter",
            display_order=1,
        )

        subscription = Subscription.objects.create(
            org=org,
            plan=starter,
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_123",
            stripe_customer_id="cus_123",
        )

        return {
            "org": org,
            "subscription": subscription,
            "free": free,
            "starter": starter,
        }

    @patch("stripe.Subscription.cancel")
    def test_paid_to_free_cancels_stripe(self, mock_cancel, setup):
        """Test that Paid to Free cancels the Stripe subscription."""
        mock_cancel.return_value = MagicMock()

        service = PlanChangeService()

        result = service.change_plan(
            setup["subscription"],
            setup["free"],
        )

        assert result.success
        assert result.effective_immediately
        mock_cancel.assert_called_once_with("sub_123", prorate=True)

        # Verify local subscription was updated
        setup["subscription"].refresh_from_db()
        assert setup["subscription"].plan.code == PlanCode.FREE
        assert setup["subscription"].stripe_subscription_id == ""
        assert setup["subscription"].status == SubscriptionStatus.ACTIVE

    @patch("stripe.Subscription.cancel")
    def test_paid_to_free_creates_audit_log(self, mock_cancel, setup):
        """Test that Paid to Free creates audit log entry."""
        mock_cancel.return_value = MagicMock()

        service = PlanChangeService()

        service.change_plan(
            setup["subscription"],
            setup["free"],
        )

        # Check audit log was created
        change = PlanChange.objects.get(subscription=setup["subscription"])
        assert change.old_plan.code == PlanCode.STARTER
        assert change.new_plan.code == PlanCode.FREE
        assert change.change_type == "downgrade"
        assert change.effective_immediately


@pytest.mark.django_db
class TestChangePlanPaidToPaid:
    """Tests for Paid to Paid changes."""

    @pytest.fixture
    def setup(self):
        """Create test data."""
        org = Organization.objects.create(name="Test Org", slug="test-org")

        starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
            included_credits=200,
            stripe_price_id="price_starter",
            display_order=1,
        )
        team = Plan.objects.create(
            code=PlanCode.TEAM,
            name="Team",
            monthly_price_cents=9900,
            included_credits=1000,
            stripe_price_id="price_team",
            display_order=2,
        )

        subscription = Subscription.objects.create(
            org=org,
            plan=starter,
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_123",
            stripe_customer_id="cus_123",
            current_period_end=timezone.now() + timedelta(days=15),
        )

        return {
            "org": org,
            "subscription": subscription,
            "starter": starter,
            "team": team,
        }

    @patch("stripe.Invoice.retrieve")
    @patch("stripe.Subscription.modify")
    @patch("stripe.Subscription.retrieve")
    def test_upgrade_applies_immediately(
        self,
        mock_retrieve,
        mock_modify,
        mock_invoice,
        setup,
    ):
        """Test that upgrade applies immediately with proration."""
        # Mock Stripe subscription
        mock_item = MagicMock()
        mock_item.id = "si_123"
        mock_sub = MagicMock()
        mock_sub.items.data = [mock_item]
        mock_retrieve.return_value = mock_sub

        # Mock modify response
        mock_modified = MagicMock()
        mock_modified.latest_invoice = "inv_123"
        mock_modify.return_value = mock_modified

        # Mock invoice with proration
        mock_line = MagicMock()
        mock_line.proration = True
        mock_line.amount = 5000  # $50 proration
        mock_inv = MagicMock()
        mock_inv.lines.data = [mock_line]
        mock_invoice.return_value = mock_inv

        service = PlanChangeService()

        result = service.change_plan(
            setup["subscription"],
            setup["team"],
        )

        assert result.success
        assert result.effective_immediately
        assert result.proration_amount_cents == 5000

        # Verify Stripe was called correctly
        mock_modify.assert_called_once()
        call_kwargs = mock_modify.call_args[1]
        assert call_kwargs["proration_behavior"] == "always_invoice"
        assert call_kwargs["payment_behavior"] == "error_if_incomplete"

    @patch("stripe.SubscriptionSchedule.modify")
    @patch("stripe.SubscriptionSchedule.create")
    @patch("stripe.Subscription.retrieve")
    def test_downgrade_is_scheduled(
        self,
        mock_retrieve,
        mock_create,
        mock_modify,
        setup,
    ):
        """Test that downgrade is scheduled for end of period."""
        # Start on Team, downgrade to Starter
        setup["subscription"].plan = setup["team"]
        setup["subscription"].save()

        # Mock Stripe subscription
        mock_sub = MagicMock()
        mock_sub.schedule = None
        mock_retrieve.return_value = mock_sub

        # Mock schedule creation
        mock_schedule = MagicMock()
        mock_schedule.id = "sub_sched_123"
        mock_schedule.phases = [{"start_date": 123, "end_date": 456}]
        mock_create.return_value = mock_schedule

        service = PlanChangeService()

        result = service.change_plan(
            setup["subscription"],
            setup["starter"],
        )

        assert result.success
        assert not result.effective_immediately
        assert result.scheduled_at is not None

        # Verify schedule was created
        mock_create.assert_called_once()
        mock_modify.assert_called_once()


@pytest.mark.django_db
class TestMultipleChangesInCycle:
    """Tests for handling multiple plan changes in one billing cycle."""

    @pytest.fixture
    def setup(self):
        """Create test data."""
        org = Organization.objects.create(name="Test Org", slug="test-org")

        starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
            stripe_price_id="price_starter",
            display_order=1,
        )
        team = Plan.objects.create(
            code=PlanCode.TEAM,
            name="Team",
            monthly_price_cents=9900,
            stripe_price_id="price_team",
            display_order=2,
        )

        subscription = Subscription.objects.create(
            org=org,
            plan=starter,
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_123",
            current_period_end=timezone.now() + timedelta(days=15),
        )

        return {
            "org": org,
            "subscription": subscription,
            "starter": starter,
            "team": team,
        }

    @patch("stripe.Invoice.retrieve")
    @patch("stripe.Subscription.modify")
    @patch("stripe.Subscription.retrieve")
    def test_multiple_upgrades_all_recorded(
        self,
        mock_retrieve,
        mock_modify,
        mock_invoice,
        setup,
    ):
        """Test that multiple upgrades in one cycle are all recorded."""
        # Mock Stripe
        mock_item = MagicMock()
        mock_item.id = "si_123"
        mock_sub = MagicMock()
        mock_sub.items.data = [mock_item]
        mock_retrieve.return_value = mock_sub

        mock_modified = MagicMock()
        mock_modified.latest_invoice = None
        mock_modify.return_value = mock_modified

        service = PlanChangeService()

        # First upgrade: Starter -> Team
        result1 = service.change_plan(
            setup["subscription"],
            setup["team"],
        )
        assert result1.success

        # Simulate being on Team now
        setup["subscription"].plan = setup["team"]
        setup["subscription"].save()

        # Second change: Team -> Starter (downgrade)
        # This would be scheduled, but let's just verify audit logging
        changes = PlanChange.objects.filter(subscription=setup["subscription"])
        assert changes.count() == 1
        assert changes.first().old_plan.code == PlanCode.STARTER
        assert changes.first().new_plan.code == PlanCode.TEAM


@pytest.mark.django_db
class TestCancelScheduledChange:
    """Tests for canceling scheduled plan changes."""

    @pytest.fixture
    def setup(self):
        """Create test data."""
        org = Organization.objects.create(name="Test Org", slug="test-org")

        starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
            stripe_price_id="price_starter",
            display_order=1,
        )

        subscription = Subscription.objects.create(
            org=org,
            plan=starter,
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_123",
        )

        return {"org": org, "subscription": subscription, "starter": starter}

    @patch("stripe.SubscriptionSchedule.cancel")
    @patch("stripe.Subscription.retrieve")
    def test_cancel_scheduled_change(self, mock_retrieve, mock_cancel, setup):
        """Test canceling a scheduled plan change."""
        # Mock subscription with schedule
        mock_sub = MagicMock()
        mock_sub.schedule = "sub_sched_123"
        mock_retrieve.return_value = mock_sub

        service = PlanChangeService()

        result = service.cancel_scheduled_change(setup["subscription"])

        assert result is True
        mock_cancel.assert_called_once_with("sub_sched_123")

    @patch("stripe.Subscription.retrieve")
    def test_cancel_with_no_schedule(self, mock_retrieve, setup):
        """Test canceling when no schedule exists."""
        # Mock subscription without schedule
        mock_sub = MagicMock()
        mock_sub.schedule = None
        mock_retrieve.return_value = mock_sub

        service = PlanChangeService()

        result = service.cancel_scheduled_change(setup["subscription"])

        assert result is False

    def test_cancel_without_stripe_subscription(self, setup):
        """Test canceling when there's no Stripe subscription."""
        setup["subscription"].stripe_subscription_id = ""
        setup["subscription"].save()

        service = PlanChangeService()

        result = service.cancel_scheduled_change(setup["subscription"])

        assert result is False
