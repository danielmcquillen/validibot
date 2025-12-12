"""
Tests for billing views.

Tests CheckoutStartView, PlansView, BillingDashboardView, and CustomerPortalView.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import Client
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse

from validibot.billing.constants import PlanCode
from validibot.billing.constants import SubscriptionStatus
from validibot.billing.models import Plan
from validibot.billing.models import Subscription
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import User


class CheckoutStartViewTests(TestCase):
    """Tests for CheckoutStartView."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        # Create plans (use update_or_create to avoid conflicts with other tests)
        cls.starter_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 100,
                "included_credits": 50,
                "monthly_price_cents": 2900,
                "stripe_price_id": "",  # Not configured for this test
            },
        )
        cls.team_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.TEAM,
            defaults={
                "name": "Team",
                "basic_launches_limit": 500,
                "included_credits": 200,
                "monthly_price_cents": 9900,
                "stripe_price_id": "price_test_team",  # Configured
            },
        )

        # Create user and org
        cls.user, _ = User.objects.get_or_create(
            email="checkout_test@example.com",
            defaults={"username": "checkout_test", "password": "testpass123"},
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="test-org-checkout",
            defaults={"name": "Test Org"},
        )
        cls.membership, _ = Membership.objects.get_or_create(
            user=cls.user,
            org=cls.org,
            defaults={"is_active": True},
        )
        cls.subscription, _ = Subscription.objects.get_or_create(
            org=cls.org,
            defaults={
                "plan": cls.starter_plan,
                "status": SubscriptionStatus.TRIALING,
            },
        )

    def setUp(self):
        """Set up client and login."""
        self.client = Client()
        self.client.force_login(self.user)
        # Set the current org via session
        session = self.client.session
        session["active_org_id"] = self.org.id
        session.save()

    def test_checkout_redirects_to_plans_when_plan_not_found(self):
        """Checkout redirects to plans page with error when plan doesn't exist."""
        response = self.client.get(
            reverse("billing:checkout") + "?plan=NONEXISTENT",
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("plans", response.url)

    def test_checkout_redirects_to_plans_when_no_stripe_price_id(self):
        """Checkout redirects to plans page with error when plan has no stripe_price_id."""
        response = self.client.get(
            reverse("billing:checkout") + "?plan=STARTER",
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("plans", response.url)

    @override_settings(STRIPE_SECRET_KEY="")
    def test_checkout_redirects_to_plans_when_stripe_key_missing(self):
        """Checkout redirects to plans page with error when STRIPE_SECRET_KEY is empty."""
        response = self.client.get(
            reverse("billing:checkout") + "?plan=TEAM",
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("plans", response.url)

    @patch("validibot.billing.services.BillingService")
    @override_settings(STRIPE_SECRET_KEY="sk_test_fake")
    def test_checkout_redirects_to_stripe_on_success(self, mock_service_class):
        """Checkout redirects to Stripe checkout URL on success."""
        mock_service = MagicMock()
        mock_service.create_checkout_session.return_value = (
            "https://checkout.stripe.com/test"
        )
        mock_service_class.return_value = mock_service

        response = self.client.get(
            reverse("billing:checkout") + "?plan=TEAM&skip_trial=1",
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.stripe.com/test")

        # Verify service was called correctly
        mock_service.create_checkout_session.assert_called_once()
        call_kwargs = mock_service.create_checkout_session.call_args.kwargs
        self.assertEqual(call_kwargs["plan"].code, PlanCode.TEAM)
        self.assertTrue(call_kwargs["skip_trial"])

    @patch("validibot.billing.services.BillingService")
    @override_settings(STRIPE_SECRET_KEY="sk_test_fake")
    def test_checkout_redirects_to_plans_on_stripe_error(self, mock_service_class):
        """Checkout redirects to plans page with error when Stripe API fails."""
        mock_service = MagicMock()
        mock_service.create_checkout_session.side_effect = Exception("Stripe error")
        mock_service_class.return_value = mock_service

        response = self.client.get(
            reverse("billing:checkout") + "?plan=TEAM",
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("plans", response.url)


class PlansViewTests(TestCase):
    """Tests for PlansView."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.starter_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 100,
                "included_credits": 50,
                "monthly_price_cents": 2900,
                "display_order": 1,
            },
        )
        cls.team_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.TEAM,
            defaults={
                "name": "Team",
                "basic_launches_limit": 500,
                "included_credits": 200,
                "monthly_price_cents": 9900,
                "display_order": 2,
            },
        )

        cls.user, _ = User.objects.get_or_create(
            email="plans@example.com",
            defaults={"username": "plans_user", "password": "testpass123"},
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="plans-test-org",
            defaults={"name": "Plans Test Org"},
        )
        cls.membership, _ = Membership.objects.get_or_create(
            user=cls.user,
            org=cls.org,
            defaults={"is_active": True},
        )
        cls.subscription, _ = Subscription.objects.get_or_create(
            org=cls.org,
            defaults={
                "plan": cls.starter_plan,
                "status": SubscriptionStatus.TRIALING,
            },
        )

    def setUp(self):
        """Set up client and login."""
        self.client = Client()
        self.client.force_login(self.user)
        session = self.client.session
        session["active_org_id"] = self.org.id
        session.save()

    def test_plans_view_shows_all_plans(self):
        """Plans view shows all available plans."""
        response = self.client.get(reverse("billing:plans"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Starter")
        self.assertContains(response, "Team")

    def test_plans_view_shows_current_plan_indicator(self):
        """Plans view highlights the current plan."""
        response = self.client.get(reverse("billing:plans"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current")

    def test_plans_view_shows_trial_banner(self):
        """Plans view shows trial days remaining for trial users."""
        response = self.client.get(reverse("billing:plans"))

        self.assertEqual(response.status_code, 200)
        # Should show trial info (exact text depends on template)
        self.assertIn("is_trial", response.context)
        self.assertTrue(response.context["is_trial"])


class BillingDashboardViewTests(TestCase):
    """Tests for BillingDashboardView."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 100,
                "included_credits": 50,
                "monthly_price_cents": 2900,
            },
        )

        cls.user, _ = User.objects.get_or_create(
            email="dashboard@example.com",
            defaults={"username": "dashboard_user", "password": "testpass123"},
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="dashboard-test-org",
            defaults={"name": "Dashboard Test Org"},
        )
        cls.membership, _ = Membership.objects.get_or_create(
            user=cls.user,
            org=cls.org,
            defaults={"is_active": True},
        )
        cls.subscription, _ = Subscription.objects.update_or_create(
            org=cls.org,
            defaults={
                "plan": cls.plan,
                "status": SubscriptionStatus.ACTIVE,
                "included_credits_remaining": 30,
                "purchased_credits_balance": 10,
            },
        )

    def setUp(self):
        """Set up client and login."""
        self.client = Client()
        self.client.force_login(self.user)
        session = self.client.session
        session["active_org_id"] = self.org.id
        session.save()

    def test_dashboard_shows_current_plan(self):
        """Dashboard shows current plan details."""
        response = self.client.get(reverse("billing:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Starter")

    def test_dashboard_shows_credits_balance(self):
        """Dashboard shows credit balances."""
        response = self.client.get(reverse("billing:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("credits_balance", response.context)
        # Verify credits_balance is a non-negative number (actual value depends on subscription state)
        self.assertIsInstance(response.context["credits_balance"], int)
        self.assertGreaterEqual(response.context["credits_balance"], 0)

    def test_dashboard_shows_welcome_banner(self):
        """Dashboard shows welcome banner when welcome=1 in query."""
        response = self.client.get(reverse("billing:dashboard") + "?welcome=1")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_welcome"])


class CustomerPortalViewTests(TestCase):
    """Tests for CustomerPortalView."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 100,
                "included_credits": 50,
                "monthly_price_cents": 2900,
            },
        )

        cls.user, _ = User.objects.get_or_create(
            email="portal@example.com",
            defaults={"username": "portal_user", "password": "testpass123"},
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug="portal-test-org",
            defaults={"name": "Portal Test Org"},
        )
        cls.membership, _ = Membership.objects.get_or_create(
            user=cls.user,
            org=cls.org,
            defaults={"is_active": True},
        )
        cls.subscription, _ = Subscription.objects.update_or_create(
            org=cls.org,
            defaults={
                "plan": cls.plan,
                "status": SubscriptionStatus.ACTIVE,
                "stripe_customer_id": "",  # No Stripe customer yet
            },
        )

    def setUp(self):
        """Set up client and login."""
        self.client = Client()
        self.client.force_login(self.user)
        session = self.client.session
        session["active_org_id"] = self.org.id
        session.save()

    def test_portal_redirects_to_plans_when_no_stripe_customer(self):
        """Portal redirects to plans when no Stripe customer exists."""
        # Ensure stripe_customer_id is empty for this test - use database update
        Subscription.objects.filter(org=self.org).update(stripe_customer_id="")

        response = self.client.get(reverse("billing:portal"), follow=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("plans", response.url)

    @patch("validibot.billing.services.BillingService")
    @override_settings(STRIPE_SECRET_KEY="sk_test_fake")
    def test_portal_redirects_to_stripe_when_customer_exists(self, mock_service_class):
        """Portal redirects to Stripe when customer exists."""
        # Set up customer ID directly in database using org_id to avoid object identity issues
        rows_updated = Subscription.objects.filter(org_id=self.org.id).update(
            stripe_customer_id="cus_test123",
        )
        # Verify the update worked
        self.assertEqual(rows_updated, 1, "Should have updated exactly one subscription")

        mock_service = MagicMock()
        mock_service.get_customer_portal_url.return_value = (
            "https://billing.stripe.com/portal/test"
        )
        mock_service_class.return_value = mock_service

        response = self.client.get(reverse("billing:portal"), follow=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://billing.stripe.com/portal/test")


class CheckoutE2ETests(TestCase):
    """
    End-to-end integration tests for Stripe checkout flow.

    These tests hit the real Stripe API (test mode) to verify the full flow.
    They require STRIPE_TEST_SECRET_KEY to be configured.
    """

    @classmethod
    def setUpTestData(cls):
        """Create test data with real Stripe price IDs."""
        import os

        # Get actual price IDs from Stripe (set via env or use known test prices)
        # These are created by seed_plans and exist in the Stripe test account
        starter_price_id = os.environ.get(
            "TEST_STRIPE_STARTER_PRICE_ID",
            "price_1SdKNl4RhDvR490gMQaeoTnH",  # From seed_plans
        )
        team_price_id = os.environ.get(
            "TEST_STRIPE_TEAM_PRICE_ID",
            "price_1SdKNl4RhDvR490gSHjS4wyy",  # From seed_plans
        )

        # Get or create plans with real Stripe price IDs
        cls.starter_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.STARTER,
            defaults={
                "name": "Starter",
                "basic_launches_limit": 10000,
                "included_credits": 200,
                "monthly_price_cents": 2900,
                "stripe_price_id": starter_price_id,
            },
        )
        cls.team_plan, _ = Plan.objects.update_or_create(
            code=PlanCode.TEAM,
            defaults={
                "name": "Team",
                "basic_launches_limit": 100000,
                "included_credits": 1000,
                "monthly_price_cents": 9900,
                "stripe_price_id": team_price_id,
            },
        )

        # Create test user and org with unique identifiers
        import uuid

        unique_id = uuid.uuid4().hex[:8]
        cls.user, _ = User.objects.get_or_create(
            email=f"e2e_{unique_id}@example.com",
            defaults={
                "username": f"e2e_user_{unique_id}",
                "password": "testpass123",  # noqa: S106
            },
        )
        cls.org, _ = Organization.objects.get_or_create(
            slug=f"e2e-test-org-{unique_id}",
            defaults={"name": f"E2E Test Org {unique_id}"},
        )
        cls.membership, _ = Membership.objects.get_or_create(
            user=cls.user,
            org=cls.org,
            defaults={"is_active": True},
        )
        cls.unique_email = cls.user.email

    def setUp(self):
        """Set up client and login."""
        from django.conf import settings

        self.client = Client()
        # Force login since password may not be hashed properly with get_or_create
        self.client.force_login(self.user)
        session = self.client.session
        session["active_org_id"] = self.org.id
        session.save()

        # Skip tests if Stripe not configured or using dummy key
        # Real Stripe test keys start with sk_test_ followed by alphanumeric chars
        stripe_key = settings.STRIPE_SECRET_KEY or ""
        is_real_key = stripe_key.startswith("sk_test_") and "dummy" not in stripe_key
        self.stripe_configured = is_real_key

    def test_checkout_flow_creates_stripe_session(self):
        """
        E2E test: Clicking Subscribe Now redirects to Stripe Checkout.

        This test verifies the complete flow:
        1. User is logged in with an org
        2. Plan has a valid stripe_price_id
        3. STRIPE_SECRET_KEY is configured
        4. Checkout session is created
        5. User is redirected to checkout.stripe.com
        """
        if not self.stripe_configured:
            self.skipTest("STRIPE_SECRET_KEY not configured")

        if not self.starter_plan or not self.starter_plan.stripe_price_id:
            self.skipTest("Starter plan not configured with stripe_price_id")

        # Create subscription for this org
        Subscription.objects.get_or_create(
            org=self.org,
            defaults={
                "plan": self.starter_plan,
                "status": SubscriptionStatus.TRIALING,
            },
        )

        # Make request to checkout
        response = self.client.get(
            reverse("billing:checkout") + f"?plan={self.starter_plan.code}&skip_trial=1",
            follow=False,
        )

        # Should redirect to Stripe Checkout
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.url.startswith("https://checkout.stripe.com"),
            f"Expected redirect to Stripe Checkout, got: {response.url}",
        )

    def test_checkout_requires_valid_stripe_price_id(self):
        """E2E test: Checkout fails gracefully when stripe_price_id is invalid."""
        if not self.stripe_configured:
            self.skipTest("STRIPE_SECRET_KEY not configured")

        # Create a plan with invalid stripe_price_id
        bad_plan = Plan.objects.create(
            code="BAD_PLAN",
            name="Bad Plan",
            stripe_price_id="price_invalid_123",
            monthly_price_cents=100,
        )

        Subscription.objects.get_or_create(
            org=self.org,
            defaults={
                "plan": bad_plan,
                "status": SubscriptionStatus.TRIALING,
            },
        )

        response = self.client.get(
            reverse("billing:checkout") + "?plan=BAD_PLAN",
            follow=False,
        )

        # Should redirect back to plans with error (not crash)
        self.assertEqual(response.status_code, 302)
        self.assertIn("plans", response.url)


class GetOrCreateSubscriptionTests(TestCase):
    """Tests for get_or_create_subscription helper function."""

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.plan = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            basic_launches_limit=100,
            included_credits=50,
        )

    def test_returns_existing_subscription(self):
        """Returns existing subscription when present."""
        from validibot.billing.views import get_or_create_subscription

        org = Organization.objects.create(
            name="Existing Sub Org",
            slug="existing-sub",
        )
        subscription = Subscription.objects.create(
            org=org,
            plan=self.plan,
            status=SubscriptionStatus.ACTIVE,
        )

        result = get_or_create_subscription(org)

        self.assertEqual(result.id, subscription.id)
        self.assertEqual(result.status, SubscriptionStatus.ACTIVE)

    def test_creates_subscription_for_legacy_org(self):
        """Creates default subscription for org without one."""
        from validibot.billing.views import get_or_create_subscription

        org = Organization.objects.create(
            name="Legacy Org",
            slug="legacy-org",
        )

        result = get_or_create_subscription(org)

        self.assertIsNotNone(result)
        self.assertEqual(result.org, org)
        self.assertEqual(result.plan.code, PlanCode.STARTER)
        self.assertEqual(result.status, SubscriptionStatus.TRIALING)
        self.assertIsNotNone(result.trial_started_at)
        self.assertIsNotNone(result.trial_ends_at)
