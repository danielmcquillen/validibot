"""
Management command to seed billing plans.

Creates or updates the three pricing plans (Starter, Team, Enterprise)
with limits and features from ADR-2025-11-28.

Usage:
    python manage.py seed_plans
    python manage.py seed_plans --force  # Update existing plans
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
    help = "Seed billing plans (Starter, Team, Enterprise)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Update existing plans with latest configuration",
        )

    def handle(self, *args, **options):
        force_update = options["force"]

        for plan_code, config in PLAN_CONFIG.items():
            plan, created = Plan.objects.get_or_create(
                code=plan_code,
                defaults=config,
            )

            if created:
                self.stdout.write(
                    self.style.SUCCESS(f"Created plan: {plan.name}"),
                )
            elif force_update:
                # Update all fields except stripe_price_id (preserve manual config)
                for field, value in config.items():
                    setattr(plan, field, value)
                plan.save()
                self.stdout.write(
                    self.style.SUCCESS(f"Updated plan: {plan.name}"),
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"Plan {plan.name} already exists (use --force to update)",
                    ),
                )

        self.stdout.write(self.style.SUCCESS("Done!"))

        # Show summary
        self.stdout.write("\nPlan Summary:")
        self.stdout.write("-" * 60)
        for plan in Plan.objects.all():
            launches = f"{plan.basic_launches_limit:,}"
            seats = plan.max_seats
            if plan.monthly_price_cents:
                price = f"${plan.monthly_price_cents / 100:.0f}/mo"
            else:
                price = "Contact us"
            self.stdout.write(
                f"  {plan.name}: {launches} launches, {seats} seats, {price}",
            )
