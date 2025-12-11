"""
Management command to seed billing plans and link to Stripe.

Creates or updates the three pricing plans (Starter, Team, Enterprise)
with limits and features from ADR-2025-11-28, then links them to
Stripe Prices via dj-stripe.

Stripe linking requires:
1. Products in Stripe with metadata: plan_code=STARTER, plan_code=TEAM
2. dj-stripe models synced: python manage.py djstripe_sync_models Price

Usage:
    python manage.py seed_plans              # Seed plans and link Stripe
    python manage.py seed_plans --force      # Update existing plan limits
    python manage.py seed_plans --skip-stripe  # Skip Stripe linking
    python manage.py seed_plans --list-stripe  # List available Stripe prices
"""

from django.core.management.base import BaseCommand

from validibot.billing.constants import PlanCode
from validibot.billing.models import Plan

# Plan configuration from ADR-2025-11-28
PLAN_CONFIG = {
    PlanCode.STARTER: {
        "name": "Starter",
        "description": (
            "Perfect for individuals and small teams getting started with "
            "data validation. Includes essential features to validate your "
            "building energy models."
        ),
        "basic_launches_limit": 10_000,
        "included_credits": 200,
        "max_workflows": 10,
        "max_custom_validators": 10,
        "max_seats": 2,
        "max_payload_mb": 5,
        "has_integrations": False,
        "has_audit_logs": False,
        "monthly_price_cents": 2900,  # $29
        "display_order": 1,
    },
    PlanCode.TEAM: {
        "name": "Team",
        "description": (
            "For growing teams that need more capacity and collaboration "
            "features. Includes integrations and audit logs for compliance."
        ),
        "basic_launches_limit": 100_000,
        "included_credits": 1_000,
        "max_workflows": 100,
        "max_custom_validators": 100,
        "max_seats": 10,
        "max_payload_mb": 20,
        "has_integrations": True,
        "has_audit_logs": True,
        "monthly_price_cents": 9900,  # $99
        "display_order": 2,
    },
    PlanCode.ENTERPRISE: {
        "name": "Enterprise",
        "description": (
            "For organizations with advanced requirements. Custom limits, "
            "priority support, and dedicated account management."
        ),
        "basic_launches_limit": 1_000_000,  # 10x Team
        "included_credits": 5_000,
        "max_workflows": 1_000,  # 10x Team
        "max_custom_validators": 1_000,  # 10x Team
        "max_seats": 100,  # 10x Team
        "max_payload_mb": 100,
        "has_integrations": True,
        "has_audit_logs": True,
        "monthly_price_cents": 0,  # Contact us
        "display_order": 3,
    },
}


class Command(BaseCommand):
    help = "Seed billing plans and link to Stripe prices"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Update existing plans with latest configuration",
        )
        parser.add_argument(
            "--skip-stripe",
            action="store_true",
            help="Skip Stripe price linking",
        )
        parser.add_argument(
            "--list-stripe",
            action="store_true",
            help="List available Stripe prices and exit",
        )

    def handle(self, *args, **options):
        if options["list_stripe"]:
            self._list_stripe_prices()
            return

        # Step 1: Seed plans
        self._seed_plans(force_update=options["force"])

        # Step 2: Link Stripe prices (unless skipped)
        if not options["skip_stripe"]:
            self._link_stripe_prices()

        self._show_summary()

    def _seed_plans(self, force_update: bool):
        """Create or update Plan records."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write("Step 1: Seeding Plans")
        self.stdout.write("=" * 60)

        for plan_code, config in PLAN_CONFIG.items():
            plan, created = Plan.objects.get_or_create(
                code=plan_code,
                defaults=config,
            )

            if created:
                self.stdout.write(
                    self.style.SUCCESS(f"  Created: {plan.name}"),
                )
            elif force_update:
                # Update all fields except stripe_price_id (preserve Stripe link)
                for field, value in config.items():
                    setattr(plan, field, value)
                plan.save()
                self.stdout.write(
                    self.style.SUCCESS(f"  Updated: {plan.name}"),
                )
            else:
                self.stdout.write(
                    f"  Exists: {plan.name} (use --force to update limits)",
                )

    def _link_stripe_prices(self):
        """Link Plans to Stripe Prices from dj-stripe."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write("Step 2: Linking Stripe Prices")
        self.stdout.write("=" * 60)

        try:
            from djstripe.models import Price
        except ImportError:
            self.stdout.write(
                self.style.WARNING(
                    "  dj-stripe not installed. Skipping Stripe linking.",
                ),
            )
            return

        # Get active recurring prices
        prices = Price.objects.filter(
            active=True,
            type="recurring",
        ).select_related("product")

        if not prices.exists():
            self.stdout.write(
                self.style.WARNING(
                    "  No Stripe prices found in dj-stripe.\n"
                    "  To set up Stripe:\n"
                    "    1. Create Products in Stripe with metadata: plan_code=STARTER\n"
                    "    2. Run: python manage.py djstripe_sync_models Price\n"
                    "    3. Run: python manage.py seed_plans",
                ),
            )
            return

        # Build mapping from plan_code to price
        price_by_plan_code = {}
        for price in prices:
            plan_code = price.product.metadata.get("plan_code", "").upper()
            if plan_code in [pc.value for pc in PlanCode]:
                if plan_code not in price_by_plan_code:
                    price_by_plan_code[plan_code] = price

        if not price_by_plan_code:
            self.stdout.write(
                self.style.WARNING(
                    "  No Stripe prices have plan_code metadata.\n"
                    "  Add metadata to Products in Stripe Dashboard:\n"
                    "    Key: plan_code, Value: STARTER | TEAM | ENTERPRISE",
                ),
            )
            return

        # Update Plans with matched prices
        for plan in Plan.objects.all():
            price = price_by_plan_code.get(plan.code)

            if price:
                if plan.stripe_price_id == price.id:
                    self.stdout.write(f"  {plan.name}: Already linked")
                else:
                    old = plan.stripe_price_id or "(none)"
                    plan.stripe_price_id = price.id
                    plan.save(update_fields=["stripe_price_id"])
                    self.stdout.write(
                        self.style.SUCCESS(f"  {plan.name}: {old} → {price.id}"),
                    )
            elif plan.monthly_price_cents > 0:
                self.stdout.write(
                    self.style.WARNING(
                        f"  {plan.name}: No Stripe price (add plan_code={plan.code} metadata)",
                    ),
                )
            else:
                self.stdout.write(f"  {plan.name}: Skipped (contact sales)")

    def _list_stripe_prices(self):
        """List all available Stripe Prices."""
        try:
            from djstripe.models import Price
        except ImportError:
            self.stderr.write("dj-stripe not installed.")
            return

        prices = Price.objects.filter(active=True).select_related("product")

        if not prices.exists():
            self.stdout.write(
                self.style.WARNING(
                    "No Stripe prices found.\n"
                    "Run: python manage.py djstripe_sync_models Price",
                ),
            )
            return

        self.stdout.write("\nAvailable Stripe Prices:")
        self.stdout.write("-" * 70)

        for price in prices:
            product = price.product
            plan_code = product.metadata.get("plan_code", "NOT SET")
            interval = (
                price.recurring.get("interval", "N/A")
                if price.recurring
                else "one-time"
            )
            amount = price.unit_amount / 100 if price.unit_amount else 0

            self.stdout.write(
                f"  {product.name}\n"
                f"    Price ID: {price.id}\n"
                f"    Amount: ${amount:.2f}/{interval}\n"
                f"    plan_code: {plan_code}\n",
            )

    def _show_summary(self):
        """Show final summary of all plans."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write("Summary")
        self.stdout.write("=" * 60)

        for plan in Plan.objects.all().order_by("display_order"):
            launches = f"{plan.basic_launches_limit:,}"
            seats = plan.max_seats
            price = (
                f"${plan.monthly_price_cents / 100:.0f}/mo"
                if plan.monthly_price_cents
                else "Contact us"
            )
            stripe = "✓" if plan.stripe_price_id else "✗"

            self.stdout.write(
                f"  {plan.name}: {launches} launches, {seats} seats, "
                f"{price}, Stripe: {stripe}",
            )

        # Warn about missing Stripe config
        missing = [
            p.name
            for p in Plan.objects.all()
            if not p.stripe_price_id and p.monthly_price_cents > 0
        ]
        if missing:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"Warning: {', '.join(missing)} missing Stripe price.\n"
                    "Users cannot subscribe until linked.",
                ),
            )

        self.stdout.write(self.style.SUCCESS("\nDone!"))
