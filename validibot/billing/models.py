"""
Billing models for the Validibot pricing system.

Key design decisions:
- Plan is a lookup table (Starter, Team, Enterprise) - single source of truth
- Subscription is 1:1 with Organization and has FK to Plan
- Enterprise overrides via nullable custom_* fields on Subscription
- UsageCounter evolved to support monthly billing periods

Relationship: Organization ──1:1── Subscription ──N:1── Plan
"""

from django.db import models
from model_utils.models import TimeStampedModel

from validibot.billing.constants import PlanCode
from validibot.billing.constants import SubscriptionStatus


class Plan(models.Model):
    """
    Lookup table for pricing plan configuration.

    This is the single source of truth for plan limits. Subscription has a FK
    to this model. To change limits for all Starter customers, update this row.

    Populated via data migration with initial plans (Starter, Team, Enterprise).

    Usage:
        org.subscription.plan.basic_launches_limit
    """

    code = models.CharField(
        max_length=20,
        choices=PlanCode.choices,
        primary_key=True,
        help_text="Unique plan identifier, also used as PK.",
    )
    name = models.CharField(max_length=50, help_text="Display name for the plan.")
    description = models.TextField(
        blank=True,
        help_text="Marketing description shown on pricing page.",
    )

    # Limits (null = unlimited)
    basic_launches_limit = models.IntegerField(
        null=True,
        blank=True,
        help_text="Monthly basic workflow launches. Null = unlimited.",
    )
    included_credits = models.IntegerField(
        default=0,
        help_text="Credits included per billing period for advanced workflows.",
    )
    max_workflows = models.IntegerField(
        null=True,
        blank=True,
        help_text="Maximum number of workflows. Null = unlimited.",
    )
    max_custom_validators = models.IntegerField(
        null=True,
        blank=True,
        help_text="Maximum custom validators. Null = unlimited.",
    )
    max_seats = models.IntegerField(
        null=True,
        blank=True,
        help_text="Maximum team members. Null = unlimited.",
    )
    max_payload_mb = models.IntegerField(
        default=5,
        help_text="Maximum upload file size in MB.",
    )

    # Feature flags
    has_integrations = models.BooleanField(
        default=False,
        help_text="Whether third-party integrations are enabled.",
    )
    has_audit_logs = models.BooleanField(
        default=False,
        help_text="Whether audit log access is enabled.",
    )

    # Pricing (for display purposes - actual charges via Stripe)
    monthly_price_cents = models.IntegerField(
        default=0,
        help_text="Monthly price in cents for display. 0 = contact sales.",
    )
    stripe_price_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Stripe Price ID for subscription checkout.",
    )

    # Display order for plan comparison UI
    display_order = models.IntegerField(
        default=0,
        help_text="Order in which plans appear on pricing page.",
    )

    class Meta:
        ordering = ["display_order"]

    def __str__(self) -> str:
        return self.name

    @property
    def monthly_price_dollars(self) -> int:
        """Monthly price in whole dollars for display."""
        return self.monthly_price_cents // 100 if self.monthly_price_cents else 0


class Subscription(TimeStampedModel):
    """
    Billing subscription for an organization.

    Plan limits are accessed via the FK: subscription.plan.basic_launches_limit
    Enterprise orgs may have custom overrides via the custom_* fields.

    This model combines:
    - Which plan the org is on (FK to Plan)
    - Subscription lifecycle state (status)
    - Stripe integration IDs
    - Credit balances (runtime state)
    - Enterprise custom overrides

    Usage:
        # Get a limit (respects Enterprise overrides)
        limit = org.subscription.get_effective_limit("max_seats")

        # Check total credits available
        credits = org.subscription.total_credits_balance
    """

    org = models.OneToOneField(
        "users.Organization",
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,  # Never delete a plan with subscriptions
        related_name="subscriptions",
    )
    status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.TRIALING,
    )

    # Signup intent tracking
    # Stores the plan the user selected from pricing page during signup.
    # Useful for: analytics, pre-selecting plan on trial-expired page,
    # and understanding conversion intent vs actual subscription.
    intended_plan = models.ForeignKey(
        Plan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="intended_subscriptions",
        help_text="Plan user selected from pricing page (may differ from actual plan).",
    )

    # Trial tracking
    trial_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the trial began.",
    )
    trial_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the trial expires. Checked by middleware.",
    )

    # Stripe integration
    stripe_customer_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Stripe Customer ID (cus_xxx).",
    )
    stripe_subscription_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Stripe Subscription ID (sub_xxx).",
    )

    # Credit balances (runtime state, not limits)
    included_credits_remaining = models.IntegerField(
        default=0,
        help_text="Remaining credits from plan's monthly allotment.",
    )
    purchased_credits_balance = models.IntegerField(
        default=0,
        help_text="Credits purchased via credit packs.",
    )

    # Billing period tracking
    current_period_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Start of current billing period.",
    )
    current_period_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text="End of current billing period.",
    )

    # Enterprise custom overrides (null = use plan defaults from FK)
    # These allow negotiated limits for Enterprise customers
    custom_basic_launches_limit = models.IntegerField(
        null=True,
        blank=True,
        help_text="Enterprise override for basic_launches_limit.",
    )
    custom_included_credits = models.IntegerField(
        null=True,
        blank=True,
        help_text="Enterprise override for included_credits.",
    )
    custom_max_seats = models.IntegerField(
        null=True,
        blank=True,
        help_text="Enterprise override for max_seats.",
    )

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["stripe_customer_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.org.name} - {self.plan.name} ({self.status})"

    @property
    def total_credits_balance(self) -> int:
        """Total available credits (included + purchased)."""
        return self.included_credits_remaining + self.purchased_credits_balance

    @property
    def has_custom_limits(self) -> bool:
        """True if any Enterprise override is set."""
        return any([
            self.custom_basic_launches_limit is not None,
            self.custom_included_credits is not None,
            self.custom_max_seats is not None,
        ])

    def get_effective_limit(self, field: str) -> int | None:
        """
        Get effective limit for a field, checking custom override first.

        For Enterprise customers with negotiated limits, the custom_* field
        takes precedence. For all others, returns the value from the Plan.

        Args:
            field: The limit field name (e.g., "basic_launches_limit", "max_seats")

        Returns:
            The effective limit value, or None if unlimited.

        Usage:
            limit = subscription.get_effective_limit("basic_launches_limit")
            if limit is None:
                # Unlimited
            elif current_usage >= limit:
                # At limit
        """
        custom_field = f"custom_{field}"
        custom_value = getattr(self, custom_field, None)
        if custom_value is not None:
            return custom_value
        return getattr(self.plan, field)


class CreditPurchase(TimeStampedModel):
    """
    Audit trail for credit pack purchases.

    Each purchase increases subscription.purchased_credits_balance.
    Credits are consumed from included first, then purchased.
    """

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="credit_purchases",
    )
    credits = models.IntegerField(help_text="Number of credits purchased.")
    amount_cents = models.IntegerField(help_text="Amount charged in cents.")
    stripe_payment_intent_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Stripe PaymentIntent ID for this purchase.",
    )

    def __str__(self) -> str:
        return f"{self.subscription.org.name}: {self.credits} credits"


class PlanChange(TimeStampedModel):
    """
    Audit log for plan changes.

    Records all upgrades, downgrades, and plan transitions for:
    - Billing reconciliation
    - Customer support history
    - Analytics on plan movement
    """

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="plan_changes",
    )
    old_plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name="changes_from",
    )
    new_plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name="changes_to",
    )
    change_type = models.CharField(
        max_length=20,
        help_text="Type of change: upgrade, downgrade, or lateral.",
    )
    effective_immediately = models.BooleanField(
        default=True,
        help_text="Whether change took effect immediately or was scheduled.",
    )
    scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When scheduled change will take effect.",
    )
    proration_amount_cents = models.IntegerField(
        null=True,
        blank=True,
        help_text="Proration amount charged/credited in cents.",
    )
    notes = models.TextField(
        blank=True,
        help_text="Additional context for the change.",
    )

    class Meta:
        ordering = ["-created"]

    def __str__(self) -> str:
        return (
            f"{self.subscription.org.name}: "
            f"{self.old_plan.name} → {self.new_plan.name}"
        )


class UsageCounter(TimeStampedModel):
    """
    Track usage for quota enforcement and reporting.

    Originally tracked daily usage. Now supports monthly billing periods
    via the period_start/period_end fields for monthly billing alignment.
    For backwards compatibility, date field still supported for daily tracking.
    """

    org = models.ForeignKey(
        "users.Organization",
        on_delete=models.CASCADE,
        related_name="usage_counters",
    )

    # Daily tracking (legacy, still works)
    date = models.DateField(
        null=True,
        blank=True,
        help_text="Date for daily counters. Use period fields for monthly.",
    )

    # Monthly billing period tracking
    period_start = models.DateField(
        null=True,
        blank=True,
        help_text="Start of billing period (for monthly tracking).",
    )
    period_end = models.DateField(
        null=True,
        blank=True,
        help_text="End of billing period (for monthly tracking).",
    )

    # Usage metrics
    basic_launches = models.IntegerField(
        default=0,
        help_text="Number of basic workflow launches this period.",
    )
    advanced_launches = models.IntegerField(
        default=0,
        help_text="Number of advanced workflow launches this period.",
    )
    credits_consumed = models.IntegerField(
        default=0,
        help_text="Total credits consumed this period.",
    )

    # Legacy fields (kept for backwards compat during migration)
    submissions = models.IntegerField(default=0)
    run_minutes = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["org", "date"]),
            models.Index(fields=["org", "period_start"]),
        ]
        # Allow either daily or monthly tracking
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(date__isnull=False)
                    | models.Q(period_start__isnull=False)
                ),
                name="usage_counter_has_period",
            ),
        ]

    def __str__(self) -> str:
        if self.date:
            return f"{self.org.name}: {self.date}"
        return f"{self.org.name}: {self.period_start} to {self.period_end}"


# Note: OrgQuota model has been removed.
# Plan limits are now stored in the Plan model (lookup table).
# Access via: org.subscription.plan.basic_launches_limit
# The OrgQuota table will be dropped in the migration.
