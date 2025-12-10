# Pricing System Implementation Plan

**Created:** 2025-12-11
**Status:** Pending approval
**Based on:** ADR-2025-11-28 (with modifications: no free tier, 2-week trial instead)

---

## Scope

This plan covers **Phase 1: Core Billing Infrastructure** with minimal viable enforcement. Later phases (usage monitoring dashboards, compute tracking, auto-purchase) are deferred.

**Key Clarifications:**
- No free tier. Only paid plans (Starter, Team, Enterprise) with a 2-week trial for new orgs.
- Trial expiry = hard block, redirect to conversion page
- Enterprise = contact-us only, manual provisioning
- No existing data concerns (clean slate)

---

## Current State Analysis

### Existing Models (billing app)

| Model | Fields | Notes |
|-------|--------|-------|
| `OrgQuota` | `max_submissions_per_day`, `max_run_minutes_per_day`, `artifact_retention_days` | **TO BE DELETED** - limits should come from plan, not per-org storage |
| `UsageCounter` | `org`, `date`, `submissions`, `run_minutes` | Keep but evolve for monthly periods |

### Key Files

- `validibot/billing/models.py` - Current models (41 lines, minimal)
- `validibot/billing/urls.py` - Empty urlpatterns
- `validibot/users/models.py` - Organization, User, Membership models
- `validibot/validations/constants.py` - `ADVANCED_VALIDATION_TYPES` already defined
- `validibot/validations/services/validation_run.py` - `ValidationRunService.launch()` is the enforcement point

### Design Principle: DRY Limits

Plan limits are defined in a `Plan` model (lookup table), not per-org. This avoids:
- Data redundancy (every Starter org having identical values)
- Update pain (changing Starter limits = updating every org)
- Source-of-truth confusion

The `Subscription` model has a FK to `Plan`. Access limits directly:
```python
org.subscription.plan.basic_launches_limit
```

Enterprise orgs with negotiated limits use nullable override fields on `Subscription`.

### Best Practices Applied (Django + Stripe)

Based on comprehensive research of Django SaaS billing patterns:

#### Key Decision: dj-stripe vs Custom Integration

**Recommendation: Use [dj-stripe](https://dj-stripe.dev/)** - the mature Django library for Stripe integration.

| Aspect | dj-stripe | Custom Integration |
|--------|-----------|-------------------|
| Webhook handling | Automatic with idempotency, retries | Manual implementation |
| Data sync | Automatic to Django models | Manual sync required |
| Stripe models | Pre-built Django ORM models | Build your own |
| Maintenance | Library handles Stripe API changes | Self-maintained |
| Signals | Django signals for events | Custom event handling |

**Why dj-stripe:**
- Handles keeping data in sync with Stripe automatically
- Provides Django ORM access to all Stripe objects (Customer, Subscription, Invoice, etc.)
- Built-in webhook signature verification and idempotency
- 10+ years of production use
- Avoids sync bugs: "imagine if someone signed up for $10/month and got billed $20 because data was out of sync"

#### Core Best Practices

1. **Stripe is the source of truth** - Our Django app holds a read-only copy synced via dj-stripe
2. **Use Stripe Checkout** (not custom payment forms) - Simplifies PCI compliance, handles 3D Secure
3. **Dual provisioning paths** - Use BOTH checkout callback AND webhook for reliability (users may close browser)
4. **B2B: Associate subscriptions with Organizations, not Users** - Personnel changes; team-level billing is more stable
5. **Use Stripe Customer Portal** for self-service management (payment methods, cancel, etc.)
6. **Feature gating in code, not Stripe metadata** - Easier testing, version control, rollback
7. **Idempotent webhooks** - dj-stripe handles this, but also store `stripe_event_id` to prevent duplicates

#### Critical Webhook Events to Handle

| Event | When | Action |
|-------|------|--------|
| `checkout.session.completed` | Payment successful | Provision access (backup to callback) |
| `customer.subscription.trial_will_end` | 3 days before trial ends | Notify user, verify payment method |
| `customer.subscription.updated` | Plan change, coupon, etc. | Sync subscription state |
| `customer.subscription.deleted` | Subscription ends | Revoke access |
| `invoice.paid` | Successful payment | Reset credits, extend access |
| `invoice.payment_failed` | Payment failed | Notify customer, start dunning |

#### Trial Lifecycle (important for our 2-week trial)

1. `trialing` → Safe to provision access
2. `customer.subscription.trial_will_end` fires 3 days before
3. Trial ends → Stripe attempts payment
4. If payment fails: `active` → `past_due` (1 hour grace) → `canceled` (after 3 days)
5. If no payment method: Status remains `trialing` then becomes `past_due`

**Our approach:** Middleware checks `trial_ends_at` and redirects expired trials to conversion page.

#### Testing Best Practices

- Use Stripe CLI: `stripe listen --forward-to localhost:8000/billing/stripe/webhook/`
- Use Stripe Test Clocks to simulate time passage for trial expiry
- Mock webhook events in unit tests

---

## Implementation Steps

### Step 1: Add Constants

**File:** `validibot/billing/constants.py` (NEW)

```python
from django.db import models
from django.utils.translation import gettext_lazy as _


class PlanCode(models.TextChoices):
    """Plan codes - used as primary key for Plan model."""
    STARTER = "STARTER", _("Starter")
    TEAM = "TEAM", _("Team")
    ENTERPRISE = "ENTERPRISE", _("Enterprise")


class SubscriptionStatus(models.TextChoices):
    """Subscription lifecycle states."""
    TRIALING = "TRIALING", _("Trial")
    TRIAL_EXPIRED = "TRIAL_EXPIRED", _("Trial Expired")
    ACTIVE = "ACTIVE", _("Active")
    PAST_DUE = "PAST_DUE", _("Past Due")
    CANCELED = "CANCELED", _("Canceled")
    SUSPENDED = "SUSPENDED", _("Suspended")
```

---

### Step 2: Create Billing Models

**File:** `validibot/billing/models.py`

**Changes:**
1. **DELETE `OrgQuota`** - limits come from `Plan` model, not per-org storage
2. Add new `Plan` model (lookup table for plan configuration)
3. Add new `Subscription` model (primary billing record per org, FK to Plan)
4. Add `CreditPurchase` model (audit trail for purchases)
5. Evolve `UsageCounter` to support monthly billing periods

**New Plan Model (Lookup Table):**

```python
class Plan(models.Model):
    """
    Lookup table for pricing plan configuration.

    This is the single source of truth for plan limits. Subscription has a FK
    to this model. To change limits for all Starter customers, update this row.

    Populated via data migration with initial plans.
    """
    code = models.CharField(
        max_length=20,
        choices=PlanCode.choices,
        primary_key=True,
    )
    name = models.CharField(max_length=50)
    description = models.TextField(blank=True)

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
    max_workflows = models.IntegerField(null=True, blank=True)
    max_custom_validators = models.IntegerField(null=True, blank=True)
    max_seats = models.IntegerField(null=True, blank=True)
    max_payload_mb = models.IntegerField(default=5)

    # Feature flags
    has_integrations = models.BooleanField(default=False)
    has_audit_logs = models.BooleanField(default=False)

    # Pricing (for display purposes - actual charges via Stripe)
    monthly_price_cents = models.IntegerField(default=0)
    stripe_price_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Stripe Price ID for subscription checkout.",
    )

    # Display order for plan comparison UI
    display_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["display_order"]

    def __str__(self):
        return self.name
```

**New Subscription Model:**

```python
class Subscription(TimeStampedModel):
    """
    Billing subscription for an organization.

    Plan limits are accessed via the FK: subscription.plan.basic_launches_limit
    Enterprise orgs may have custom overrides via the custom_* fields.
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

    # Trial tracking
    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)

    # Stripe integration
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True)

    # Credit balances (runtime state, not limits)
    included_credits_remaining = models.IntegerField(default=0)
    purchased_credits_balance = models.IntegerField(default=0)

    # Billing period
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)

    # Enterprise custom overrides (null = use plan defaults from FK)
    custom_basic_launches_limit = models.IntegerField(null=True, blank=True)
    custom_included_credits = models.IntegerField(null=True, blank=True)
    custom_max_seats = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["stripe_customer_id"]),
        ]

    def __str__(self):
        return f"{self.org.name} - {self.plan.name} ({self.status})"

    @property
    def total_credits_balance(self) -> int:
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

        Usage: subscription.get_effective_limit("basic_launches_limit")
        """
        custom_field = f"custom_{field}"
        custom_value = getattr(self, custom_field, None)
        if custom_value is not None:
            return custom_value
        return getattr(self.plan, field)
```

---

### Step 3: Add Data Migration for Plans

**File:** `validibot/billing/migrations/0003_plan_subscription_data.py` (NEW - data migration)

Seed the Plan lookup table with initial plans:

```python
def seed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")

    Plan.objects.create(
        code="STARTER",
        name="Starter",
        description="For individuals and small teams getting started.",
        basic_launches_limit=10_000,
        included_credits=200,
        max_workflows=10,
        max_custom_validators=10,
        max_seats=2,
        max_payload_mb=5,
        has_integrations=False,
        has_audit_logs=False,
        monthly_price_cents=2900,  # $29/month
        display_order=1,
    )

    Plan.objects.create(
        code="TEAM",
        name="Team",
        description="For growing teams with advanced needs.",
        basic_launches_limit=100_000,
        included_credits=1_000,
        max_workflows=100,
        max_custom_validators=100,
        max_seats=10,
        max_payload_mb=20,
        has_integrations=True,
        has_audit_logs=True,
        monthly_price_cents=9900,  # $99/month
        display_order=2,
    )

    Plan.objects.create(
        code="ENTERPRISE",
        name="Enterprise",
        description="Custom solutions for large organizations.",
        basic_launches_limit=None,  # Unlimited
        included_credits=5_000,
        max_workflows=None,
        max_custom_validators=None,
        max_seats=None,
        max_payload_mb=100,
        has_integrations=True,
        has_audit_logs=True,
        monthly_price_cents=0,  # Contact sales
        display_order=3,
    )
```

---

### Step 4: Create Subscription on Org Creation

**File:** `validibot/users/models.py`

Modify `ensure_personal_workspace()` to also create a Subscription with trial status:

```python
# After creating Organization...
from validibot.billing.models import Plan, Subscription
from validibot.billing.constants import PlanCode, SubscriptionStatus

starter_plan = Plan.objects.get(code=PlanCode.STARTER)
Subscription.objects.create(
    org=personal_org,
    plan=starter_plan,
    status=SubscriptionStatus.TRIALING,
    trial_started_at=timezone.now(),
    trial_ends_at=timezone.now() + timedelta(days=14),
    included_credits_remaining=starter_plan.included_credits,
)
```

**Alternative:** Use a Django signal on Organization creation.

---

### Step 5: Install and Configure dj-stripe

**File:** `pyproject.toml`

```toml
[project.dependencies]
# ... existing deps
dj-stripe = "^2.8"
```

**File:** `config/settings/base.py`

```python
INSTALLED_APPS = [
    # ... existing apps
    "djstripe",
]

# Stripe / dj-stripe
# ------------------------------------------------------------------------------
STRIPE_TEST_SECRET_KEY = env("STRIPE_TEST_SECRET_KEY", default="")
STRIPE_LIVE_SECRET_KEY = env("STRIPE_LIVE_SECRET_KEY", default="")
STRIPE_TEST_PUBLIC_KEY = env("STRIPE_TEST_PUBLIC_KEY", default="")
STRIPE_LIVE_PUBLIC_KEY = env("STRIPE_LIVE_PUBLIC_KEY", default="")
STRIPE_LIVE_MODE = env.bool("STRIPE_LIVE_MODE", default=False)

# dj-stripe settings
DJSTRIPE_WEBHOOK_SECRET = env("DJSTRIPE_WEBHOOK_SECRET", default="")
DJSTRIPE_FOREIGN_KEY_TO_FIELD = "id"  # Use Stripe IDs as FKs
DJSTRIPE_USE_NATIVE_JSONFIELD = True
```

**File:** `config/urls.py`

```python
urlpatterns = [
    # ... existing urls
    path("stripe/", include("djstripe.urls", namespace="djstripe")),
]
```

**Initial sync from Stripe:**

```bash
# Run migrations for dj-stripe models
uv run python manage.py migrate djstripe

# Sync products and prices from Stripe dashboard
uv run python manage.py djstripe_sync_models Product Price
```

---

### Step 6: Webhook Handlers (using dj-stripe)

**File:** `validibot/billing/webhooks.py` (NEW)

dj-stripe provides automatic webhook handling. We add custom handlers using decorators:

```python
from djstripe import webhooks
from djstripe.models import Subscription as DJStripeSubscription

from validibot.billing.models import Subscription
from validibot.billing.constants import SubscriptionStatus


@webhooks.handler("checkout.session.completed")
def handle_checkout_completed(event, **kwargs):
    """
    Provision access after successful checkout.

    This is a backup to the checkout success redirect - handles cases
    where users close browser after payment but before redirect completes.
    """
    session = event.data["object"]
    org_id = session.get("client_reference_id")
    if not org_id:
        return

    # dj-stripe already synced the Stripe subscription
    # Update our Subscription model to reflect active status
    subscription = Subscription.objects.filter(org_id=org_id).first()
    if subscription:
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save(update_fields=["status"])


@webhooks.handler("customer.subscription.trial_will_end")
def handle_trial_ending(event, **kwargs):
    """
    Fires 3 days before trial ends.

    Notify user and verify payment method is on file.
    """
    stripe_sub = event.data["object"]
    # Send notification email
    # Check if payment method exists on customer


@webhooks.handler("customer.subscription.updated")
def handle_subscription_updated(event, **kwargs):
    """Sync subscription changes (plan upgrades, etc.)."""
    stripe_sub = event.data["object"]
    # dj-stripe auto-syncs the Stripe subscription
    # Update our Subscription model if needed


@webhooks.handler("customer.subscription.deleted")
def handle_subscription_deleted(event, **kwargs):
    """Revoke access when subscription ends."""
    stripe_sub = event.data["object"]
    customer_id = stripe_sub.get("customer")

    subscription = Subscription.objects.filter(
        stripe_customer_id=customer_id
    ).first()
    if subscription:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.save(update_fields=["status"])


@webhooks.handler("invoice.paid")
def handle_invoice_paid(event, **kwargs):
    """
    Successful payment - reset credits for the new billing period.
    """
    invoice = event.data["object"]
    customer_id = invoice.get("customer")

    subscription = Subscription.objects.filter(
        stripe_customer_id=customer_id
    ).first()
    if subscription:
        # Reset included credits to plan baseline
        subscription.included_credits_remaining = subscription.plan.included_credits
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save()


@webhooks.handler("invoice.payment_failed")
def handle_payment_failed(event, **kwargs):
    """
    Payment failed - notify customer, start dunning flow.
    """
    invoice = event.data["object"]
    # Send notification email
    # Update subscription status to PAST_DUE
```

**Note:** These handlers are automatically registered by dj-stripe. Put this file in a module that's imported (e.g., via `apps.py` ready method).

---

### Step 7: Billing Service

**File:** `validibot/billing/services.py` (NEW)

```python
import stripe
from django.conf import settings
from djstripe.models import Customer

from validibot.billing.models import Plan, Subscription


class BillingService:
    """
    Service for Stripe billing operations.

    Uses Stripe Checkout for payments (not custom forms).
    Stripe Customer Portal for self-service management.
    """

    def get_or_create_stripe_customer(self, org) -> Customer:
        """Get or create Stripe customer for an organization."""
        subscription = org.subscription

        if subscription.stripe_customer_id:
            return Customer.objects.get(id=subscription.stripe_customer_id)

        # Create new Stripe customer
        customer = Customer.create(
            email=self._get_billing_email(org),
            name=org.name,
            metadata={"org_id": str(org.id)},
        )

        subscription.stripe_customer_id = customer.id
        subscription.save(update_fields=["stripe_customer_id"])

        return customer

    def create_checkout_session(
        self,
        org,
        plan: Plan,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """
        Create Stripe Checkout session for subscription.

        Returns the checkout session URL to redirect user to.
        """
        customer = self.get_or_create_stripe_customer(org)

        session = stripe.checkout.Session.create(
            customer=customer.id,
            mode="subscription",
            line_items=[{
                "price": plan.stripe_price_id,
                "quantity": 1,
            }],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(org.id),  # For webhook handler
            metadata={"org_id": str(org.id), "plan_code": plan.code},
        )

        return session.url

    def get_customer_portal_url(self, org, return_url: str) -> str:
        """
        Get Stripe Customer Portal URL for self-service management.

        Users can update payment methods, view invoices, cancel, etc.
        """
        customer = self.get_or_create_stripe_customer(org)

        session = stripe.billing_portal.Session.create(
            customer=customer.id,
            return_url=return_url,
        )

        return session.url

    def _get_billing_email(self, org) -> str:
        """Get billing contact email for organization."""
        # Return owner's email or first admin
        from validibot.users.models import Membership
        from validibot.users.constants import RoleCode

        owner = Membership.objects.filter(
            org=org,
            is_active=True,
            membership_roles__role__code=RoleCode.OWNER,
        ).select_related("user").first()

        if owner:
            return owner.user.email

        # Fallback to any admin
        admin = Membership.objects.filter(
            org=org,
            is_active=True,
        ).select_related("user").first()

        return admin.user.email if admin else ""
```

---

### Step 8: Basic Enforcement (Metering)

**File:** `validibot/billing/metering.py` (NEW)

```python
class BasicWorkflowLimitExceeded(Exception):
    def __init__(self, detail: str, code: str = "basic_limit_exceeded"):
        self.detail = detail
        self.code = code

class InsufficientCreditsError(Exception):
    def __init__(self, detail: str, code: str = "insufficient_credits"):
        self.detail = detail
        self.code = code

class TrialExpiredError(Exception):
    def __init__(self, detail: str, code: str = "trial_expired"):
        self.detail = detail
        self.code = code

class BasicWorkflowMeter:
    def check_and_increment(self, org: Organization) -> None:
        """
        Check trial/subscription status and quota. Raises if blocked.

        Limits accessed via subscription.plan FK (with Enterprise overrides).
        """
        sub = org.subscription

        if sub.status == SubscriptionStatus.TRIAL_EXPIRED:
            raise TrialExpiredError(...)

        # Get limit from Plan model (or Enterprise override)
        limit = sub.get_effective_limit("basic_launches_limit")

        if limit is not None:
            counter = self._get_or_create_monthly_counter(org)
            if counter.basic_launches >= limit:
                raise BasicWorkflowLimitExceeded(...)
            counter.basic_launches += 1
            counter.save()

class AdvancedWorkflowMeter:
    def check_balance(self, org: Organization) -> int:
        """Return remaining credits."""
        return org.subscription.total_credits_balance

    def consume_credits(self, org: Organization, credits: int) -> None:
        """Deduct credits after run completion (included first, then purchased)."""


class SeatEnforcer:
    """
    Enforce seat limits when adding members to an org.

    Replaces OrgQuota.max_seats - now uses subscription.get_effective_limit("max_seats").
    """
    def check_can_add_member(self, org: Organization) -> None:
        """Raises SeatLimitExceeded if org is at max seats."""
        limit = org.subscription.get_effective_limit("max_seats")
        if limit is None:
            return  # Unlimited (Enterprise)

        current_seats = org.membership_set.filter(is_active=True).count()
        if current_seats >= limit:
            raise SeatLimitExceeded(
                f"Organization has reached its limit of {limit} seats. "
                "Upgrade your plan to add more team members."
            )
```

**Enforcement Points:**
- **Seat limits**: Checked in `Membership.save()` or invite acceptance flow
- **Basic workflow launches**: Checked in `ValidationRunService.launch()`
- **Advanced workflow credits**: Checked in `ValidationRunService.launch()`
- **Max workflows**: Checked in `Workflow.save()` (deferred to Phase 2)
- **Max custom validators**: Checked in custom validator creation (deferred to Phase 2)

---

### Step 9: Integrate Enforcement into Launch

**File:** `validibot/validations/services/validation_run.py`

Modify `ValidationRunService.launch()`:

```python
from validibot.billing.metering import BasicWorkflowMeter, AdvancedWorkflowMeter
from validibot.billing.metering import BasicWorkflowLimitExceeded, TrialExpiredError

def launch(self, ...):
    # NEW: Check billing/quota before creating run
    try:
        if workflow.is_advanced:  # Need to add this property
            AdvancedWorkflowMeter().check_balance(org)
        else:
            BasicWorkflowMeter().check_and_increment(org)
    except (BasicWorkflowLimitExceeded, TrialExpiredError) as e:
        # Return error response
        ...
```

---

### Step 10: Add Workflow Classification

**File:** `validibot/workflows/models.py`

Add property to Workflow:

```python
@property
def is_advanced(self) -> bool:
    """True if workflow uses any high-compute validators."""
    from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
    return self.steps.filter(
        validator__validation_type__in=ADVANCED_VALIDATION_TYPES
    ).exists()
```

---

### Step 11: Simple Billing Dashboard View

**File:** `validibot/billing/views.py`

```python
class BillingDashboardView(LoginRequiredMixin, OrgMixin, TemplateView):
    template_name = "billing/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        subscription = self.org.subscription
        context["subscription"] = subscription
        context["plan"] = subscription.plan  # FK access to Plan model
        context["is_trial"] = subscription.status == SubscriptionStatus.TRIALING
        context["trial_days_remaining"] = ...
        context["all_plans"] = Plan.objects.all()  # For plan comparison
        return context
```

**File:** `validibot/templates/billing/dashboard.html` (NEW)

Basic dashboard showing:
- Current plan and status
- Trial countdown (if applicable)
- Usage summary
- Upgrade/manage subscription buttons

---

### Step 12: Trial Expiry Middleware

**File:** `validibot/billing/middleware.py` (NEW)

```python
from django.shortcuts import redirect
from django.utils import timezone

class TrialExpiryMiddleware:
    """
    Redirect users with expired trials to the conversion page.

    Checks subscription status on each request. If trial has expired,
    redirects to /billing/trial-expired/ (except for billing URLs).
    """

    EXEMPT_PATHS = [
        "/billing/",
        "/accounts/logout/",
        "/static/",
        "/media/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            return self.get_response(request)

        # Skip exempt paths
        if any(request.path.startswith(p) for p in self.EXEMPT_PATHS):
            return self.get_response(request)

        # Check subscription status
        org = getattr(request.user, 'current_org', None)
        if org and hasattr(org, 'subscription'):
            sub = org.subscription
            if sub.status == SubscriptionStatus.TRIALING:
                if sub.trial_ends_at and sub.trial_ends_at < timezone.now():
                    sub.status = SubscriptionStatus.TRIAL_EXPIRED
                    sub.save(update_fields=["status"])

            if sub.status == SubscriptionStatus.TRIAL_EXPIRED:
                return redirect("billing:trial-expired")

        return self.get_response(request)
```

**File:** `config/settings/base.py`

Add to MIDDLEWARE (after authentication middleware):

```python
MIDDLEWARE = [
    # ... existing middleware ...
    "validibot.billing.middleware.TrialExpiryMiddleware",
]
```

---

### Step 13: URL Wiring

**File:** `config/urls.py`

Ensure billing URLs are included:

```python
path("billing/", include("validibot.billing.urls", namespace="billing")),
```

---

### Step 14: Migrations

Create migration for new models:

```bash
uv run python manage.py makemigrations billing
```

This will create:
- New `Plan` model (lookup table)
- New `Subscription` model (with FK to Plan)
- New `CreditPurchase` model
- Modified `UsageCounter` (add period fields)
- Delete `OrgQuota` model

Then create the data migration (Step 3) to seed Plan records.

---

## File Summary

| File | Action | Description |
|------|--------|-------------|
| `validibot/billing/constants.py` | CREATE | PlanCode, SubscriptionStatus enums |
| `validibot/billing/models.py` | MODIFY | Delete OrgQuota, add Plan, Subscription, CreditPurchase |
| `validibot/billing/migrations/0003_*.py` | CREATE | Data migration to seed Plan lookup table |
| `validibot/billing/services.py` | CREATE | BillingService (Checkout, Portal, customer mgmt) |
| `validibot/billing/webhooks.py` | CREATE | dj-stripe webhook handlers (decorators) |
| `validibot/billing/metering.py` | CREATE | Enforcement classes |
| `validibot/billing/middleware.py` | CREATE | Trial expiry redirect middleware |
| `validibot/billing/views.py` | MODIFY | Add BillingDashboardView, TrialExpiredView |
| `validibot/billing/urls.py` | MODIFY | Add dashboard, trial-expired URLs |
| `validibot/users/models.py` | MODIFY | Create Subscription on org creation |
| `validibot/workflows/models.py` | MODIFY | Add is_advanced property |
| `validibot/validations/services/validation_run.py` | MODIFY | Add billing enforcement |
| `config/settings/base.py` | MODIFY | Add dj-stripe config, INSTALLED_APPS, middleware |
| `config/urls.py` | MODIFY | Include djstripe.urls for webhook endpoint |
| `pyproject.toml` | MODIFY | Add dj-stripe dependency |
| `validibot/templates/billing/dashboard.html` | CREATE | Basic billing UI |
| `validibot/templates/billing/trial_expired.html` | CREATE | Trial expiry conversion page |

---

## Deferred to Later Phases

- Extended usage dashboards with charts
- Compute tracking (Modal callback integration)
- Credit pack auto-purchase
- Audit logs feature gating
- Integrations feature gating
- Warning threshold notifications
- Payment failure dunning flow

---

## Testing Strategy

1. **Unit tests:** Subscription model methods, metering logic
2. **Integration tests:** Webhook handling with mock Stripe events
3. **Manual testing:** Full checkout flow with Stripe test mode

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Stripe webhook signature verification fails | Test with Stripe CLI locally first |
| Trial expiry edge cases | Clear state machine for subscription status |

---

## Design Decisions (Confirmed)

1. **Trial expiry behavior:** Hard block. Users with expired trials cannot access any features - they're redirected to a "Trial Complete" conversion page customized to their usage/situation.

2. **Existing data:** Not a concern - system hasn't gone live yet. Clean slate, no backwards compatibility needed.

3. **Enterprise tier:** Contact-us only. No self-serve Stripe checkout. Manual provisioning by admin.

---

## Trial Expiry Flow

When a user's trial expires and they haven't subscribed:

1. Middleware checks `subscription.status` and `subscription.trial_ends_at`
2. If `status == TRIALING` and `trial_ends_at < now()`:
   - Update status to `TRIAL_EXPIRED`
   - Redirect ALL requests (except billing URLs) to `/billing/trial-expired/`
3. Trial expired page shows:
   - "Your 14-day trial has ended"
   - Usage summary from trial period
   - Plan comparison with pricing
   - "Subscribe Now" CTA → Stripe Checkout
   - "Contact Sales" for Enterprise inquiries
