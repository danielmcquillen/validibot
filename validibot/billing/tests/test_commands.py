"""
Tests for billing management commands.

Tests seed_plans and link_stripe_prices command functionality.
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
        call_command("seed_plans", stdout=out)

        # Verify all plans created
        self.assertEqual(Plan.objects.count(), 3)
        self.assertTrue(Plan.objects.filter(code=PlanCode.STARTER).exists())
        self.assertTrue(Plan.objects.filter(code=PlanCode.TEAM).exists())
        self.assertTrue(Plan.objects.filter(code=PlanCode.ENTERPRISE).exists())

        output = out.getvalue()
        self.assertIn("Created plan: Starter", output)
        self.assertIn("Created plan: Team", output)
        self.assertIn("Created plan: Enterprise", output)

    def test_does_not_overwrite_existing_plans_without_force(self):
        """seed_plans doesn't overwrite plans without --force flag."""
        # Create existing plan with custom value
        Plan.objects.create(
            code=PlanCode.STARTER,
            name="Custom Starter",  # Different from default
            basic_launches_limit=999,
        )

        out = StringIO()
        call_command("seed_plans", stdout=out)

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
        call_command("seed_plans", "--force", stdout=out)

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
        call_command("seed_plans", "--force", stdout=out)

        starter = Plan.objects.get(code=PlanCode.STARTER)
        self.assertEqual(starter.stripe_price_id, "price_existing")

    def test_shows_plan_summary(self):
        """seed_plans shows summary of all plans."""
        out = StringIO()
        call_command("seed_plans", stdout=out)

        output = out.getvalue()
        self.assertIn("Plan Summary", output)
        self.assertIn("Starter:", output)
        self.assertIn("Team:", output)
        self.assertIn("Enterprise:", output)

    def test_warns_about_missing_stripe_config(self):
        """seed_plans warns about paid plans without stripe_price_id."""
        out = StringIO()
        call_command("seed_plans", stdout=out)

        output = out.getvalue()
        # Should warn about Starter and Team missing Stripe config
        self.assertIn("Warning", output)
        self.assertIn("no stripe_price_id", output)

    def test_plan_limits_are_correct(self):
        """seed_plans creates plans with correct ADR-defined limits."""
        call_command("seed_plans")

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


class LinkStripePricesCommandTests(TestCase):
    """Tests for the link_stripe_prices management command."""

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

    @patch("validibot.billing.management.commands.link_stripe_prices.Command._link_prices")
    def test_dry_run_does_not_save(self, mock_link):
        """--dry-run calls _link_prices with dry_run=True."""
        out = StringIO()
        call_command("link_stripe_prices", "--dry-run", stdout=out)

        mock_link.assert_called_once()
        # Second arg should be dry_run=True
        self.assertTrue(mock_link.call_args[0][1])

    @patch("validibot.billing.management.commands.link_stripe_prices.Command._list_prices")
    def test_list_shows_prices(self, mock_list):
        """--list calls _list_prices."""
        out = StringIO()
        call_command("link_stripe_prices", "--list", stdout=out)

        mock_list.assert_called_once()

    def test_links_prices_from_djstripe(self):
        """link_stripe_prices updates Plan.stripe_price_id from dj-stripe."""
        # Create mock Price model
        mock_price_model = MagicMock()

        # Create mock prices with product metadata
        mock_starter_price = MagicMock()
        mock_starter_price.id = "price_starter_123"
        mock_starter_price.active = True
        mock_starter_price.type = "recurring"
        mock_starter_price.unit_amount = 2900
        mock_starter_price.recurring = {"interval": "month"}
        mock_starter_price.product = MagicMock()
        mock_starter_price.product.metadata = {"plan_code": "STARTER"}

        mock_team_price = MagicMock()
        mock_team_price.id = "price_team_456"
        mock_team_price.active = True
        mock_team_price.type = "recurring"
        mock_team_price.unit_amount = 9900
        mock_team_price.recurring = {"interval": "month"}
        mock_team_price.product = MagicMock()
        mock_team_price.product.metadata = {"plan_code": "TEAM"}

        mock_price_model.objects.filter.return_value.select_related.return_value = [
            mock_starter_price,
            mock_team_price,
        ]
        mock_price_model.objects.filter.return_value.select_related.return_value.exists.return_value = True  # noqa: E501

        # Import and run the command's _link_prices method directly
        from validibot.billing.management.commands.link_stripe_prices import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = MagicMock()
        cmd.style.SUCCESS = lambda x: x
        cmd.style.WARNING = lambda x: x

        cmd._link_prices(mock_price_model, dry_run=False)

        # Verify plans were updated
        self.starter.refresh_from_db()
        self.team.refresh_from_db()

        self.assertEqual(self.starter.stripe_price_id, "price_starter_123")
        self.assertEqual(self.team.stripe_price_id, "price_team_456")

    def test_skips_enterprise_with_no_price(self):
        """Enterprise plan (contact us) is skipped without error."""
        mock_price_model = MagicMock()
        mock_price_model.objects.filter.return_value.select_related.return_value = []
        mock_price_model.objects.filter.return_value.select_related.return_value.exists.return_value = False  # noqa: E501

        from validibot.billing.management.commands.link_stripe_prices import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = MagicMock()
        cmd.style.WARNING = lambda x: x

        # Should not raise
        cmd._link_prices(mock_price_model, dry_run=False)

        # Enterprise should still have no price_id
        self.enterprise.refresh_from_db()
        self.assertEqual(self.enterprise.stripe_price_id, "")
