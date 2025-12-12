"""
Tests for billing management commands.

Tests seed_plans command functionality including Stripe linking.
"""

from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from validibot.billing.constants import PlanCode
from validibot.billing.models import Plan


class SeedPlansCommandTests(TestCase):
    """Tests for the seed_plans management command."""

    def test_creates_all_plans_when_none_exist(self):
        """seed_plans creates all three plans when database is empty."""
        out = StringIO()
        call_command("seed_plans", "--skip-stripe", stdout=out)

        # Verify all plans created
        self.assertEqual(Plan.objects.count(), 3)
        self.assertTrue(Plan.objects.filter(code=PlanCode.STARTER).exists())
        self.assertTrue(Plan.objects.filter(code=PlanCode.TEAM).exists())
        self.assertTrue(Plan.objects.filter(code=PlanCode.ENTERPRISE).exists())

        output = out.getvalue()
        self.assertIn("Created: Starter", output)
        self.assertIn("Created: Team", output)
        self.assertIn("Created: Enterprise", output)

    def test_does_not_overwrite_existing_plans_without_force(self):
        """seed_plans doesn't overwrite plans without --force flag."""
        # Create existing plan with custom value
        Plan.objects.create(
            code=PlanCode.STARTER,
            name="Custom Starter",  # Different from default
            basic_launches_limit=999,
        )

        out = StringIO()
        call_command("seed_plans", "--skip-stripe", stdout=out)

        # Verify original values preserved
        starter = Plan.objects.get(code=PlanCode.STARTER)
        self.assertEqual(starter.name, "Custom Starter")
        self.assertEqual(starter.basic_launches_limit, 999)

    def test_updates_plans_with_force_flag(self):
        """seed_plans --force updates all plans with configured values."""
        # Create plan with custom values
        Plan.objects.create(
            code=PlanCode.STARTER,
            name="Custom Starter",
            basic_launches_limit=999,
        )

        out = StringIO()
        call_command("seed_plans", "--force", "--skip-stripe", stdout=out)

        # Verify values updated to configured defaults
        starter = Plan.objects.get(code=PlanCode.STARTER)
        self.assertEqual(starter.name, "Starter")
        self.assertEqual(starter.basic_launches_limit, 10_000)

    def test_preserves_stripe_price_id_on_force_update(self):
        """seed_plans --force preserves existing stripe_price_id."""
        Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            stripe_price_id="price_existing",
        )

        out = StringIO()
        call_command("seed_plans", "--force", "--skip-stripe", stdout=out)

        starter = Plan.objects.get(code=PlanCode.STARTER)
        self.assertEqual(starter.stripe_price_id, "price_existing")

    def test_shows_plan_summary(self):
        """seed_plans shows summary of all plans."""
        out = StringIO()
        call_command("seed_plans", "--skip-stripe", stdout=out)

        output = out.getvalue()
        self.assertIn("Summary", output)
        self.assertIn("Starter:", output)
        self.assertIn("Team:", output)
        self.assertIn("Enterprise:", output)

    def test_warns_about_missing_stripe_config(self):
        """seed_plans warns about paid plans without stripe_price_id."""
        out = StringIO()
        call_command("seed_plans", "--skip-stripe", stdout=out)

        output = out.getvalue()
        # Should warn about Starter and Team missing Stripe config
        self.assertIn("Warning", output)
        self.assertIn("missing Stripe price", output)

    def test_plan_limits_are_correct(self):
        """seed_plans creates plans with correct ADR-defined limits."""
        call_command("seed_plans", "--skip-stripe")

        starter = Plan.objects.get(code=PlanCode.STARTER)
        team = Plan.objects.get(code=PlanCode.TEAM)
        enterprise = Plan.objects.get(code=PlanCode.ENTERPRISE)

        # Starter limits
        self.assertEqual(starter.basic_launches_limit, 10_000)
        self.assertEqual(starter.included_credits, 200)
        self.assertEqual(starter.max_seats, 2)
        self.assertEqual(starter.max_workflows, 10)
        self.assertEqual(starter.monthly_price_cents, 2900)  # $29

        # Team limits
        self.assertEqual(team.basic_launches_limit, 100_000)
        self.assertEqual(team.included_credits, 1_000)
        self.assertEqual(team.max_seats, 10)
        self.assertEqual(team.max_workflows, 100)
        self.assertEqual(team.monthly_price_cents, 9900)  # $99
        self.assertTrue(team.has_integrations)
        self.assertTrue(team.has_audit_logs)

        # Enterprise limits
        self.assertEqual(enterprise.basic_launches_limit, 1_000_000)
        self.assertEqual(enterprise.included_credits, 5_000)
        self.assertEqual(enterprise.max_seats, 100)
        self.assertEqual(enterprise.monthly_price_cents, 0)  # Contact us


class SeedPlansStripeLinkingTests(TestCase):
    """Tests for Stripe linking in seed_plans command."""

    def setUp(self):
        """Create test plans."""
        self.starter = Plan.objects.create(
            code=PlanCode.STARTER,
            name="Starter",
            monthly_price_cents=2900,
        )
        self.team = Plan.objects.create(
            code=PlanCode.TEAM,
            name="Team",
            monthly_price_cents=9900,
        )
        self.enterprise = Plan.objects.create(
            code=PlanCode.ENTERPRISE,
            name="Enterprise",
            monthly_price_cents=0,  # Contact us
        )

    @patch("validibot.billing.management.commands.seed_plans.Command._link_stripe_prices")
    def test_skip_stripe_flag_skips_linking(self, mock_link):
        """--skip-stripe skips Stripe price linking."""
        out = StringIO()
        call_command("seed_plans", "--skip-stripe", stdout=out)

        mock_link.assert_not_called()

    @patch("validibot.billing.management.commands.seed_plans.Command._list_stripe_prices")
    def test_list_stripe_shows_prices(self, mock_list):
        """--list-stripe calls _list_stripe_prices."""
        out = StringIO()
        call_command("seed_plans", "--list-stripe", stdout=out)

        mock_list.assert_called_once()

    def test_links_prices_from_djstripe(self):
        """seed_plans updates Plan.stripe_price_id from dj-stripe."""
        # Create mock Price objects
        mock_starter_price = MagicMock()
        mock_starter_price.id = "price_starter_123"
        mock_starter_price.product = MagicMock()
        mock_starter_price.product.metadata = {"plan_code": "STARTER"}

        mock_team_price = MagicMock()
        mock_team_price.id = "price_team_456"
        mock_team_price.product = MagicMock()
        mock_team_price.product.metadata = {"plan_code": "TEAM"}

        # Mock the Price model query
        mock_prices = MagicMock()
        mock_prices.exists.return_value = True
        mock_prices.__iter__ = lambda self: iter([mock_starter_price, mock_team_price])

        with patch("djstripe.models.Price") as mock_price_model:
            mock_price_model.objects.filter.return_value.select_related.return_value = mock_prices

            # Import and run the command's _link_stripe_prices method
            from validibot.billing.management.commands.seed_plans import Command

            cmd = Command()
            cmd.stdout = StringIO()
            cmd.style = MagicMock()
            cmd.style.SUCCESS = lambda x: x
            cmd.style.WARNING = lambda x: x

            cmd._link_stripe_prices()

        # Verify plans were updated
        self.starter.refresh_from_db()
        self.team.refresh_from_db()

        self.assertEqual(self.starter.stripe_price_id, "price_starter_123")
        self.assertEqual(self.team.stripe_price_id, "price_team_456")

    def test_skips_enterprise_with_no_price(self):
        """Enterprise plan (contact us) is skipped without error."""
        # Mock empty price results
        mock_prices = MagicMock()
        mock_prices.exists.return_value = False

        with patch("djstripe.models.Price") as mock_price_model:
            mock_price_model.objects.filter.return_value.select_related.return_value = mock_prices

            from validibot.billing.management.commands.seed_plans import Command

            cmd = Command()
            cmd.stdout = StringIO()
            cmd.style = MagicMock()
            cmd.style.WARNING = lambda x: x

            # Should not raise
            cmd._link_stripe_prices()

        # Enterprise should still have no price_id
        self.enterprise.refresh_from_db()
        self.assertEqual(self.enterprise.stripe_price_id, "")
