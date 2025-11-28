# ADR-2025-11-28: Pricing System and Stripe Integration

**Status:** Proposed (2025-11-28)  
**Owners:** Platform / Billing / Infrastructure  
**Related ADRs:** 2025-11-28-public-workflow-access  
**Related docs:** `dev_docs/overview/how_it_works.md`, `billing/models.py`

---

## Context

SimpleValidations needs a pricing and billing system that:

1. **Supports tiered plans** – Free, Starter, Team, Enterprise with different feature sets.
2. **Meters two types of usage** – Basic workflow launches (guardrail-only) and advanced workflow credits (cost-based).
3. **Integrates with Stripe** – Subscription management, credit purchases, invoicing.
4. **Provides usage visibility** – Dashboards and notifications that scale with plan tier.
5. **Enables feature throttling** – Enforce limits based on plan, with clear upgrade paths.

This ADR defines the pricing tiers, metering strategy, Stripe integration, and the infrastructure needed to implement it.

---

## Pricing Tiers

### Tier Summary

|             | Free | Starter | Team          | Enterprise       |
| ----------- | ---- | ------- | ------------- | ---------------- |
| **Price**   | $0   | $35/mo  | $100/mo       | Contact us       |
| **Seats**   | 1    | 2       | 10            | Custom           |
| **Support** | None | None    | Limited email | Priority + Slack |

### Authoring Features

|                              | Free   | Starter | Team    | Enterprise |
| ---------------------------- | ------ | ------- | ------- | ---------- |
| **Author workflows**         | ✅ Yes | ✅ Yes  | ✅ Yes  | ✅ Yes     |
| **Use basic validators**     | ✅ Yes | ✅ Yes  | ✅ Yes  | ✅ Yes     |
| **Use advanced validators**  | ❌ No  | ✅ Yes  | ✅ Yes  | ✅ Yes     |
| **Create custom validators** | 0      | 10      | 100     | Unlimited  |
| **Validation workflows**     | 2      | 10      | 100     | Unlimited  |
| **Payload size limit**       | ≤ 1 MB | ≤ 5 MB  | ≤ 20 MB | ≤ 100 MB   |

### Usage Quotas

|                               | Free   | Starter  | Team      | Enterprise  |
| ----------------------------- | ------ | -------- | --------- | ----------- |
| **Basic workflow launches**   | 200/mo | 5,000/mo | 50,000/mo | 250,000+/mo |
| **Advanced workflow credits** | 0      | 200/mo   | 1,000/mo  | 5,000+/mo   |

### Platform Features

|                        | Free  | Starter | Team     | Enterprise         |
| ---------------------- | ----- | ------- | -------- | ------------------ |
| **Integrations**       | ❌ No | ❌ No   | ✅ Yes   | ✅ Yes             |
| **Audit logs**         | ❌ No | ❌ No   | ✅ Yes   | ✅ Yes             |
| **Billing dashboard**  | None  | Basic   | Extended | Comprehensive      |
| **Analytics**          | None  | Basic   | Extended | Comprehensive      |
| **Deployment options** | Cloud | Cloud   | Cloud    | On-prem / regional |

### Overage (Credit Packs)

|                              | Free | Starter          | Team            | Enterprise  |
| ---------------------------- | ---- | ---------------- | --------------- | ----------- |
| **Purchase overage credits** | ❌   | Manual           | Manual/Auto     | Manual/Auto |
| **Pack size**                | n/a  | 100 credits      | 500 credits     | Negotiated  |
| **Pack price**               | n/a  | $10 (10¢/credit) | $25 (5¢/credit) | Negotiated  |

### Cost Analysis

**Modal compute cost per credit:**

```
1 credit = 60 seconds × 1 vCPU × 4 GiB

Modal pricing (as of 2025):
- CPU: $0.0000131 per core-second
- Memory: $0.00000222 per GiB-second

Per-second cost = 0.0000131 + (4 × 0.00000222) = $0.00002198
Per credit (60s) = 60 × $0.00002198 ≈ $0.00132 (~0.13 cents)
```

**Included credits cost us:**

| Plan       | Included Credits | Our Cost | Subscription Price | Margin on Credits |
| ---------- | ---------------- | -------- | ------------------ | ----------------- |
| Starter    | 200/mo           | ~$0.26   | $35/mo             | Essentially free  |
| Team       | 1,000/mo         | ~$1.32   | $100/mo            | Essentially free  |
| Enterprise | 5,000/mo         | ~$6.60   | $1,000+/mo         | Essentially free  |

**Overage credit pricing:**

| Plan       | Price per Credit | Our Cost | Markup |
| ---------- | ---------------- | -------- | ------ |
| Starter    | $0.10            | $0.00132 | ~75×   |
| Team       | $0.05            | $0.00132 | ~38×   |
| Enterprise | Negotiated       | $0.00132 | Custom |

This pricing is aggressive but fair. Modal costs are a rounding error; the real value is our platform, validators, and workflow orchestration.

---

## Workflow Classification: Basic vs Advanced

Each workflow is classified based on its validators. This setting should be
updated whenever a workflow is modified.

```python
# simplevalidations/workflows/constants.py

class WorkflowType(models.TextChoices):
    """
    Classification of workflows for metering purposes.

    BASIC: All validators run on Heroku dynos (subscription cost).
    ADVANCED: At least one validator runs on Modal (per-compute cost).
    """
    BASIC = "BASIC", _("Basic")
    ADVANCED = "ADVANCED", _("Advanced")
```

**Classification rules:**

| Workflow Contains                                | Classification |
| ------------------------------------------------ | -------------- |
| Only local/Heroku validators                     | BASIC          |
| Any Modal-based validator (AI, EnergyPlus, etc.) | ADVANCED       |

```python
# simplevalidations/workflows/models.py

class Workflow(FeaturedImageMixin, TimeStampedModel):

    @property
    def workflow_type(self) -> WorkflowType:
        """
        Determine workflow type based on validators used.

        A workflow is ADVANCED if any of its validators runs on Modal.
        """
        if self.validators.filter(execution_target="modal").exists():
            return WorkflowType.ADVANCED
        return WorkflowType.BASIC
```

---

## Metering Strategy

### Why Two Systems?

| Aspect               | Basic Workflows          | Advanced Workflows       |
| -------------------- | ------------------------ | ------------------------ |
| **Where it runs**    | Heroku dynos             | Modal compute            |
| **Cost structure**   | Fixed monthly            | Per-second compute       |
| **Metering**         | Count-based guardrail    | Credit-based, cost-tied  |
| **Billing approach** | Included in subscription | Credits consumed per run |

**Basic workflows** run on Heroku, which is a fixed monthly cost. We don't micro-bill CPU time because there's no incremental cost. Instead, we set per-org monthly caps as guardrails to prevent abuse.

**Advanced workflows** run on Modal, where we pay per-CPU-second. We need precise metering to recover costs and ensure fair pricing.

### Basic Workflow Metering

Simple count-based guardrail:

```python
# simplevalidations/billing/metering.py

class BasicWorkflowMeter:
    """
    Count-based metering for basic workflows.

    Basic workflows are included in the subscription. We just enforce
    monthly caps to prevent abuse and encourage upgrades.
    """

    def check_and_increment(self, org: Organization) -> None:
        """
        Check if org has basic launches remaining and increment counter.

        Raises:
            QuotaExceededError: If monthly limit reached.
        """
        counter = self._get_or_create_monthly_counter(org)
        limit = org.subscription.plan.basic_launches_per_month

        if counter.basic_launches >= limit:
            raise QuotaExceededError(
                detail=_(
                    "Monthly basic workflow limit reached (%(used)s/%(limit)s). "
                    "Upgrade your plan for more capacity."
                ) % {"used": counter.basic_launches, "limit": limit},
                code="basic_quota_exceeded",
            )

        counter.basic_launches += 1
        counter.save(update_fields=["basic_launches"])
```

### Advanced Workflow Metering (Credits)

Credit-based metering tied to actual compute cost:

#### Credit Definition

```
1 credit ≈ up to 60 seconds of 1 vCPU & 4 GiB on Modal
```

#### Credit Calculation

```python
# simplevalidations/billing/credits.py

from enum import IntEnum
from math import ceil


class ValidatorWeight(IntEnum):
    """
    Compute weight multiplier for different validator types.

    Higher weight = more expensive compute profile.
    """
    LIGHT = 1    # Simple AI parsing, document extraction
    MEDIUM = 2   # Standard simulations, complex AI
    HEAVY = 3    # EnergyPlus simulations, large models
    EXTREME = 5  # Multi-hour simulations, GPU workloads


def calculate_credits_used(
    runtime_seconds: float,
    validator_weight: ValidatorWeight,
) -> int:
    """
    Calculate credits consumed by an advanced workflow run.

    Formula: credits = ceil(runtime_seconds / 60) * validator_weight

    Examples:
        - Light AI job (45s, weight 1): ceil(45/60) * 1 = 1 credit
        - Medium sim (180s, weight 2): ceil(180/60) * 2 = 6 credits
        - Heavy sim (300s, weight 3): ceil(300/60) * 3 = 15 credits
    """
    minutes = ceil(runtime_seconds / 60)
    return minutes * validator_weight
```

#### Credit Consumption Flow

```python
# simplevalidations/billing/metering.py

class AdvancedWorkflowMeter:
    """
    Credit-based metering for advanced (Modal) workflows.

    Credits map directly to Modal compute costs, allowing us to
    recover infrastructure costs and price fairly.
    """

    def check_balance(self, org: Organization) -> int:
        """Return remaining credits for the org."""
        return org.subscription.advanced_credits_balance

    def reserve_credits(
        self,
        org: Organization,
        estimated_credits: int,
    ) -> CreditReservation:
        """
        Reserve credits before starting an advanced run.

        We reserve an estimated amount upfront, then reconcile
        after the run completes with actual usage.
        """
        balance = self.check_balance(org)

        if balance < estimated_credits:
            raise InsufficientCreditsError(
                detail=_(
                    "Insufficient credits. You have %(balance)s credits, "
                    "but this workflow typically uses %(estimated)s. "
                    "Purchase more credits or upgrade your plan."
                ) % {"balance": balance, "estimated": estimated_credits},
                code="insufficient_credits",
                credits_needed=estimated_credits - balance,
            )

        return CreditReservation.objects.create(
            org=org,
            estimated_credits=estimated_credits,
            status=CreditReservationStatus.RESERVED,
        )

    def finalize_credits(
        self,
        reservation: CreditReservation,
        actual_runtime_seconds: float,
        validator_weight: ValidatorWeight,
    ) -> int:
        """
        Finalize credit usage after run completes.

        Calculates actual credits used and adjusts the org's balance.
        """
        actual_credits = calculate_credits_used(
            runtime_seconds=actual_runtime_seconds,
            validator_weight=validator_weight,
        )

        # Deduct from org balance
        org = reservation.org
        org.subscription.advanced_credits_balance -= actual_credits
        org.subscription.save(update_fields=["advanced_credits_balance"])

        # Update reservation
        reservation.actual_credits = actual_credits
        reservation.status = CreditReservationStatus.FINALIZED
        reservation.save()

        # Check for low balance notification
        self._check_low_balance_notification(org)

        return actual_credits
```

---

## Stripe Integration

### Product Structure

```
Stripe Products:
├── sv_starter_monthly          # Starter plan subscription
│   └── $35/month
│   └── Includes: 200 advanced credits, 5k basic launches
│
├── sv_team_monthly             # Team plan subscription
│   └── $100/month
│   └── Includes: 1,000 advanced credits, 50k basic launches
│
├── sv_enterprise_monthly       # Enterprise (custom pricing)
│   └── Custom pricing (typically $1,000+/mo)
│   └── Includes: 5,000+ advanced credits, 250k basic launches
│
├── sv_credits_starter_100      # Credit pack for Starter orgs
│   └── $10 for 100 credits ($0.10/credit)
│
└── sv_credits_team_500         # Credit pack for Team/Enterprise orgs
    └── $25 for 500 credits ($0.05/credit)
```

### Subscription Model

```python
# simplevalidations/billing/models.py

class PricingPlan(models.TextChoices):
    """Available pricing plans."""
    FREE = "FREE", _("Free")
    STARTER = "STARTER", _("Starter")
    TEAM = "TEAM", _("Team")
    ENTERPRISE = "ENTERPRISE", _("Enterprise")


class Subscription(TimeStampedModel):
    """
    Organization's subscription to SimpleValidations.

    Tracks the plan, Stripe subscription ID, and current usage balances.
    Each org has exactly one active subscription.

    Credit Balance Design:
    We track TWO types of credits separately:

    1. `included_credits_balance` - Monthly included credits from subscription
       - RESET to plan baseline on each billing cycle renewal
       - Do NOT roll over - use it or lose it
       - Consumed FIRST before purchased credits

    2. `purchased_credits_balance` - One-time credit pack purchases
       - Roll over month-to-month
       - Expire 12 months after purchase (tracked via CreditPurchase records)
       - Consumed AFTER included credits are exhausted

    Total available = included_credits_balance + purchased_credits_balance
    """

    org = models.OneToOneField(
        "users.Organization",  # Lives in simplevalidations.users app
        on_delete=models.CASCADE,
        related_name="subscription",
    )

    plan = models.CharField(
        max_length=20,
        choices=PricingPlan.choices,
        default=PricingPlan.FREE,
    )

    # Stripe integration
    stripe_customer_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Stripe customer ID (cus_xxx)"),
    )
    stripe_subscription_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Stripe subscription ID (sub_xxx)"),
    )

    # Credit balances - TWO separate pools
    included_credits_balance = models.IntegerField(
        default=0,
        help_text=_("Monthly included credits. Reset on renewal, consumed first."),
    )
    purchased_credits_balance = models.IntegerField(
        default=0,
        help_text=_("Purchased credits. Roll over, expire after 12 months."),
    )

    # Billing period
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["stripe_customer_id"]),
            models.Index(fields=["stripe_subscription_id"]),
        ]

    @property
    def total_credits_balance(self) -> int:
        """Total available credits (included + purchased)."""
        return self.included_credits_balance + self.purchased_credits_balance

    def consume_credits(self, amount: int) -> None:
        """
        Consume credits, drawing from included first, then purchased.

        Raises:
            InsufficientCreditsError: If not enough credits available.
        """
        if amount > self.total_credits_balance:
            raise InsufficientCreditsError(
                f"Need {amount} credits but only {self.total_credits_balance} available."
            )

        # Consume included credits first
        from_included = min(amount, self.included_credits_balance)
        self.included_credits_balance -= from_included

        # Then consume from purchased if needed
        from_purchased = amount - from_included
        if from_purchased > 0:
            self.purchased_credits_balance -= from_purchased

        self.save(update_fields=["included_credits_balance", "purchased_credits_balance"])


class CreditPurchase(TimeStampedModel):
    """
    Record of a credit pack purchase for audit trail and expiry tracking.

    Purchased credits expire 12 months after purchase date.
    """

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="credit_purchases",
    )
    credits = models.IntegerField(help_text=_("Number of credits purchased."))
    stripe_invoice_id = models.CharField(max_length=255)
    amount_cents = models.IntegerField(help_text=_("Amount paid in cents."))
    expires_at = models.DateTimeField(
        help_text=_("When these credits expire (12 months from purchase)."),
    )

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=365)
        super().save(*args, **kwargs)
```

### Organization Model Extension

The billing system requires a few additions to the existing Organization model
in `simplevalidations.users.models`:

```python
# simplevalidations/users/models.py (additions to existing Organization model)

class Organization(models.Model):
    """
    Existing Organization model - these properties are ADDED for billing.
    """

    # ... existing fields ...

    @property
    def current_usage_counter(self) -> "UsageCounter":
        """
        Get the current billing period's usage counter.

        Returns the UsageCounter for the current billing period,
        creating one if it doesn't exist (for Free plan orgs that
        haven't gone through Stripe checkout).
        """
        from simplevalidations.billing.models import UsageCounter
        from simplevalidations.billing.plans import PLAN_LIMITS

        today = timezone.now().date()

        # Try to get existing counter for current period
        counter = self.usage_counters.filter(
            period_start__lte=today,
            period_end__gte=today,
        ).first()

        if counter:
            return counter

        # Create counter for Free tier orgs (no Stripe subscription)
        plan_limits = PLAN_LIMITS[self.subscription.plan]
        period_start = today.replace(day=1)  # First of current month

        # Calculate period end (last day of month)
        next_month = period_start.replace(day=28) + timedelta(days=4)
        period_end = next_month - timedelta(days=next_month.day)

        return UsageCounter.objects.create(
            org=self,
            period_start=period_start,
            period_end=period_end,
            basic_launches_limit=plan_limits.basic_launches_per_month or 0,
            advanced_credits_limit=plan_limits.advanced_credits_per_month,
        )
```

### Plan Configuration

```python
# simplevalidations/billing/plans.py

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PlanLimits:
    """Feature limits for a pricing plan."""

    # Authoring limits
    can_author_workflows: bool
    max_workflows: Optional[int]  # None = unlimited
    max_custom_validators: Optional[int]
    max_payload_mb: int

    # Usage limits
    basic_launches_per_month: Optional[int]
    advanced_credits_per_month: int

    # Seats
    included_seats: int

    # Features
    has_integrations: bool
    has_audit_logs: bool
    dashboard_level: str  # "none", "basic", "extended", "comprehensive"
    analytics_level: str

    # Overage
    can_purchase_credits: bool
    credits_per_pack: int
    pack_price_cents: int


PLAN_LIMITS = {
    PricingPlan.FREE: PlanLimits(
        can_author_workflows=True,
        max_workflows=2,
        max_custom_validators=0,
        max_payload_mb=1,
        basic_launches_per_month=200,
        advanced_credits_per_month=0,
        included_seats=1,
        has_integrations=False,
        has_audit_logs=False,
        dashboard_level="none",
        analytics_level="none",
        can_purchase_credits=False,
        credits_per_pack=0,
        pack_price_cents=0,
    ),
    PricingPlan.STARTER: PlanLimits(
        can_author_workflows=True,
        max_workflows=10,
        max_custom_validators=10,
        max_payload_mb=5,
        basic_launches_per_month=5_000,
        advanced_credits_per_month=200,
        included_seats=2,
        has_integrations=False,
        has_audit_logs=False,
        dashboard_level="basic",
        analytics_level="basic",
        can_purchase_credits=True,
        credits_per_pack=100,
        pack_price_cents=1000,  # $10 for 100 credits = $0.10/credit
    ),
    PricingPlan.TEAM: PlanLimits(
        can_author_workflows=True,
        max_workflows=100,
        max_custom_validators=100,
        max_payload_mb=20,
        basic_launches_per_month=50_000,
        advanced_credits_per_month=1_000,
        included_seats=10,
        has_integrations=True,
        has_audit_logs=True,
        dashboard_level="extended",
        analytics_level="extended",
        can_purchase_credits=True,
        credits_per_pack=500,
        pack_price_cents=2500,  # $25 for 500 credits = $0.05/credit
    ),
    PricingPlan.ENTERPRISE: PlanLimits(
        can_author_workflows=True,
        max_workflows=None,  # Unlimited
        max_custom_validators=None,
        max_payload_mb=100,
        basic_launches_per_month=250_000,
        advanced_credits_per_month=5_000,  # Baseline, negotiable
        included_seats=0,  # Custom, negotiated
        has_integrations=True,
        has_audit_logs=True,
        dashboard_level="comprehensive",
        analytics_level="comprehensive",
        can_purchase_credits=True,
        credits_per_pack=500,
        pack_price_cents=0,  # Negotiated
    ),
}
```

### Stripe Webhook Handling

```python
# simplevalidations/billing/webhooks.py

import stripe
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from simplevalidations.billing.models import Subscription
from simplevalidations.billing.services import BillingService


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """
    Handle Stripe webhook events.

    Events we care about:
    - checkout.session.completed: New subscription created
    - customer.subscription.updated: Plan change, renewal
    - customer.subscription.deleted: Cancellation
    - invoice.paid: Successful payment, reset credits
    - invoice.payment_failed: Payment failure
    """
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    billing_service = BillingService()

    match event["type"]:
        case "checkout.session.completed":
            billing_service.handle_checkout_completed(event["data"]["object"])

        case "customer.subscription.updated":
            billing_service.handle_subscription_updated(event["data"]["object"])

        case "customer.subscription.deleted":
            billing_service.handle_subscription_cancelled(event["data"]["object"])

        case "invoice.paid":
            billing_service.handle_invoice_paid(event["data"]["object"])

        case "invoice.payment_failed":
            billing_service.handle_payment_failed(event["data"]["object"])

    return HttpResponse(status=200)
```

### Checkout Flow

```python
# simplevalidations/billing/services.py

import stripe
from django.conf import settings
from django.urls import reverse

stripe.api_key = settings.STRIPE_SECRET_KEY


def get_billing_contact_email(org: Organization) -> str:
    """
    Get the billing contact email for an organization.

    Priority:
    1. Explicit billing_email on org (if we add it later)
    2. Owner's email (user with OWNER role)
    3. First admin's email
    4. Fallback to any active member

    Raises:
        ValueError: If no billing contact can be determined.
    """
    # Check for explicit billing email (future enhancement)
    if hasattr(org, 'billing_email') and org.billing_email:
        return org.billing_email

    # Find owner membership
    from simplevalidations.users.models import Membership
    from simplevalidations.users.constants import RoleCode

    owner_membership = Membership.objects.filter(
        org=org,
        is_active=True,
        membership_roles__role__code=RoleCode.OWNER,
    ).select_related('user').first()

    if owner_membership:
        return owner_membership.user.email

    # Fallback to any admin
    admin_membership = Membership.objects.filter(
        org=org,
        is_active=True,
        membership_roles__role__code=RoleCode.ADMIN,
    ).select_related('user').first()

    if admin_membership:
        return admin_membership.user.email

    # Last resort: any active member
    any_membership = Membership.objects.filter(
        org=org,
        is_active=True,
    ).select_related('user').first()

    if any_membership:
        return any_membership.user.email

    raise ValueError(f"No billing contact found for org {org.id}")


class BillingService:
    """
    Service for managing subscriptions and billing.
    """

    def create_checkout_session(
        self,
        org: Organization,
        plan: PricingPlan,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """
        Create a Stripe Checkout session for plan subscription.

        Returns the checkout session URL to redirect the user to.
        """
        # Get or create Stripe customer
        subscription = org.subscription
        if not subscription.stripe_customer_id:
            billing_email = get_billing_contact_email(org)
            customer = stripe.Customer.create(
                email=billing_email,
                name=org.name,
                metadata={"org_id": str(org.id)},
            )
            subscription.stripe_customer_id = customer.id
            subscription.save(update_fields=["stripe_customer_id"])

        # Get price ID for the plan
        price_id = self._get_stripe_price_id(plan)

        session = stripe.checkout.Session.create(
            customer=subscription.stripe_customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"org_id": str(org.id), "plan": plan},
        )

        return session.url

    def create_credit_purchase_session(
        self,
        org: Organization,
        quantity: int,  # Number of credit packs
        success_url: str,
        cancel_url: str,
    ) -> str:
        """
        Create a Stripe Checkout session for credit pack purchase.
        """
        plan_limits = PLAN_LIMITS[org.subscription.plan]
        price_id = self._get_credit_pack_price_id(org.subscription.plan)

        session = stripe.checkout.Session.create(
            customer=org.subscription.stripe_customer_id,
            mode="payment",
            line_items=[{"price": price_id, "quantity": quantity}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "org_id": str(org.id),
                "type": "credit_purchase",
                "credits": quantity * plan_limits.credits_per_pack,
            },
        )

        return session.url

    def handle_invoice_paid(self, invoice: dict) -> None:
        """
        Handle successful invoice payment.

        For subscription invoices (renewals):
        - RESET included_credits_balance to plan baseline
        - KEEP purchased_credits_balance unchanged (they roll over)
        - Expire any purchased credits older than 12 months

        For one-time credit purchases:
        - ADD to purchased_credits_balance
        - Record purchase with 12-month expiry
        """
        customer_id = invoice["customer"]
        subscription = Subscription.objects.get(stripe_customer_id=customer_id)

        # Check if this is a subscription renewal (has subscription ID)
        if invoice.get("subscription"):
            plan_limits = PLAN_LIMITS[subscription.plan]

            # Reset ONLY included credits (purchased credits roll over)
            subscription.included_credits_balance = (
                plan_limits.advanced_credits_per_month
            )
            subscription.current_period_start = timezone.now()
            subscription.current_period_end = (
                timezone.now() + timedelta(days=30)
            )
            subscription.save(update_fields=[
                "included_credits_balance",
                "current_period_start",
                "current_period_end",
            ])

            # Expire old purchased credits and adjust balance
            self._expire_old_purchased_credits(subscription)

            # Create new usage counter for this billing period
            self._create_usage_counter(subscription)
            return

        # Check for one-time credit pack purchases
        for line_item in invoice.get("lines", {}).get("data", []):
            metadata = line_item.get("metadata", {})
            if metadata.get("type") == "credit_purchase":
                credits = int(metadata["credits"])

                # Add to purchased credits (separate from included)
                subscription.purchased_credits_balance += credits
                subscription.save(update_fields=["purchased_credits_balance"])

                # Record the purchase with 12-month expiry
                CreditPurchase.objects.create(
                    subscription=subscription,
                    credits=credits,
                    stripe_invoice_id=invoice["id"],
                    amount_cents=line_item.get("amount", 0),
                    expires_at=timezone.now() + timedelta(days=365),
                )

    def _expire_old_purchased_credits(self, subscription: Subscription) -> None:
        """
        Expire purchased credits older than 12 months.

        We track this via CreditPurchase records. On each renewal,
        we check for expired purchases and reduce the balance.
        """
        expired_purchases = CreditPurchase.objects.filter(
            subscription=subscription,
            expires_at__lt=timezone.now(),
        )

        total_expired = sum(p.credits for p in expired_purchases)
        if total_expired > 0:
            # Reduce balance (but not below zero)
            subscription.purchased_credits_balance = max(
                0,
                subscription.purchased_credits_balance - total_expired,
            )
            subscription.save(update_fields=["purchased_credits_balance"])

            # Delete expired records
            expired_purchases.delete()

    def _create_usage_counter(self, subscription: Subscription) -> None:
        """Create a new usage counter for the billing period."""
        plan_limits = PLAN_LIMITS[subscription.plan]
        UsageCounter.objects.create(
            org=subscription.org,
            period_start=subscription.current_period_start.date(),
            period_end=subscription.current_period_end.date(),
            basic_launches_limit=plan_limits.basic_launches_per_month or 0,
            advanced_credits_limit=plan_limits.advanced_credits_per_month,
        )
```

---

## Feature Throttling

### Enforcement Points

Features are enforced at multiple points:

```python
# simplevalidations/billing/enforcement.py

from functools import wraps
from django.core.exceptions import PermissionDenied

from simplevalidations.billing.plans import PLAN_LIMITS, PricingPlan


class FeatureNotAvailable(PermissionDenied):
    """Raised when a feature is not available on the org's plan."""

    def __init__(self, feature: str, required_plan: PricingPlan):
        self.feature = feature
        self.required_plan = required_plan
        super().__init__(
            f"{feature} requires {required_plan.label} plan or higher."
        )


def require_plan_feature(feature: str):
    """
    Decorator to enforce plan-based feature access.

    Usage:
        @require_plan_feature("integrations")
        def create_integration(request, org):
            ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            org = request.org  # Assumes org is set by middleware
            plan_limits = PLAN_LIMITS[org.subscription.plan]

            # Check feature availability
            feature_attr = f"has_{feature}"
            if hasattr(plan_limits, feature_attr):
                if not getattr(plan_limits, feature_attr):
                    raise FeatureNotAvailable(
                        feature=feature,
                        required_plan=_get_minimum_plan_for_feature(feature),
                    )

            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


class PlanEnforcer:
    """
    Centralized enforcement of plan limits.
    """

    def check_can_create_workflow(self, org: Organization) -> None:
        """Check if org can create another workflow."""
        limits = PLAN_LIMITS[org.subscription.plan]

        if not limits.can_author_workflows:
            raise FeatureNotAvailable(
                feature="Workflow authoring",
                required_plan=PricingPlan.STARTER,
            )

        if limits.max_workflows is not None:
            current_count = org.workflows.count()
            if current_count >= limits.max_workflows:
                raise QuotaExceededError(
                    detail=_(
                        "You've reached the workflow limit for your plan "
                        "(%(current)s/%(max)s). Upgrade to create more."
                    ) % {"current": current_count, "max": limits.max_workflows},
                    code="workflow_limit_exceeded",
                )

    def check_payload_size(self, org: Organization, size_bytes: int) -> None:
        """Check if payload size is within plan limits."""
        limits = PLAN_LIMITS[org.subscription.plan]
        max_bytes = limits.max_payload_mb * 1024 * 1024

        if size_bytes > max_bytes:
            raise PayloadTooLargeError(
                detail=_(
                    "Payload size (%(size)s MB) exceeds your plan limit "
                    "(%(max)s MB). Upgrade for larger payloads."
                ) % {
                    "size": size_bytes / (1024 * 1024),
                    "max": limits.max_payload_mb,
                },
                code="payload_too_large",
            )
```

---

## Usage Monitoring and Notifications

### Notification Thresholds

Different plans get different notification frequencies:

| Plan       | Usage Alerts                          | Dashboard Features         |
| ---------- | ------------------------------------- | -------------------------- |
| Starter    | 80%, 100% of limits                   | Basic usage charts         |
| Team       | 50%, 75%, 90%, 100% + daily digest    | Extended analytics, trends |
| Enterprise | Custom thresholds + real-time + Slack | Full analytics, exports    |

### Usage Tracking Model

```python
# simplevalidations/billing/models.py

class UsageCounter(TimeStampedModel):
    """
    Monthly usage counters for an organization.

    A new counter is created at the start of each billing period.
    Historical counters are retained for analytics.
    """

    org = models.ForeignKey(
        "users.Organization",  # Lives in simplevalidations.users app
        on_delete=models.CASCADE,
        related_name="usage_counters",
    )

    # Billing period this counter covers
    period_start = models.DateField()
    period_end = models.DateField()

    # Basic workflow usage
    basic_launches = models.IntegerField(default=0)
    basic_launches_limit = models.IntegerField()

    # Advanced workflow usage
    advanced_credits_used = models.IntegerField(default=0)
    advanced_credits_limit = models.IntegerField()

    # Breakdown by source
    launches_by_source = models.JSONField(
        default=dict,
        help_text=_("Breakdown: {source: count}"),
    )

    # Breakdown by workflow
    launches_by_workflow = models.JSONField(
        default=dict,
        help_text=_("Breakdown: {workflow_id: count}"),
    )

    class Meta:
        unique_together = [("org", "period_start")]
        indexes = [
            models.Index(fields=["org", "period_start"]),
        ]

    @property
    def basic_usage_percent(self) -> float:
        if self.basic_launches_limit == 0:
            return 0
        return (self.basic_launches / self.basic_launches_limit) * 100

    @property
    def advanced_usage_percent(self) -> float:
        if self.advanced_credits_limit == 0:
            return 0
        return (self.advanced_credits_used / self.advanced_credits_limit) * 100
```

### Notification Service

```python
# simplevalidations/billing/notifications.py

from simplevalidations.notifications.models import Notification


class UsageNotificationService:
    """
    Send usage notifications based on plan tier.
    """

    # Thresholds by plan
    THRESHOLDS = {
        PricingPlan.STARTER: [80, 100],
        PricingPlan.TEAM: [50, 75, 90, 100],
        PricingPlan.ENTERPRISE: [50, 75, 90, 95, 100],
    }

    def check_and_notify(self, org: Organization) -> None:
        """
        Check usage levels and send notifications if thresholds crossed.
        """
        counter = org.current_usage_counter
        plan = org.subscription.plan
        thresholds = self.THRESHOLDS.get(plan, [100])

        # Check basic usage
        self._check_threshold(
            org=org,
            usage_type="basic_launches",
            usage_percent=counter.basic_usage_percent,
            thresholds=thresholds,
        )

        # Check advanced usage
        self._check_threshold(
            org=org,
            usage_type="advanced_credits",
            usage_percent=counter.advanced_usage_percent,
            thresholds=thresholds,
        )

    def _check_threshold(
        self,
        org: Organization,
        usage_type: str,
        usage_percent: float,
        thresholds: list[int],
    ) -> None:
        """Send notification if a new threshold was crossed."""
        for threshold in thresholds:
            if usage_percent >= threshold:
                # Check if we already notified for this threshold this period
                notification_key = f"{usage_type}_{threshold}"
                if self._already_notified(org, notification_key):
                    continue

                self._send_notification(org, usage_type, threshold, usage_percent)
                self._mark_notified(org, notification_key)

    def _send_notification(
        self,
        org: Organization,
        usage_type: str,
        threshold: int,
        current_percent: float,
    ) -> None:
        """Create the actual notification."""
        if threshold == 100:
            level = Notification.Level.ERROR
            title = _("Usage limit reached")
            message = _(
                "You've reached your %(usage_type)s limit. "
                "Purchase more credits or upgrade your plan to continue."
            )
        elif threshold >= 90:
            level = Notification.Level.WARNING
            title = _("Approaching usage limit")
            message = _(
                "You've used %(percent)s%% of your %(usage_type)s. "
                "Consider upgrading to avoid interruption."
            )
        else:
            level = Notification.Level.INFO
            title = _("Usage update")
            message = _("You've used %(percent)s%% of your %(usage_type)s.")

        Notification.objects.create(
            org=org,
            level=level,
            title=title,
            message=message % {
                "usage_type": usage_type.replace("_", " "),
                "percent": int(current_percent),
            },
            action_url=reverse("billing:dashboard"),
            action_label=_("View usage"),
        )
```

---

## Quota Attribution

**All usage is attributed to the workflow author's organization**, regardless of who launches the workflow.

| Scenario                                  | Quota Charged To     |
| ----------------------------------------- | -------------------- |
| Public anonymous user launches via web    | Workflow owner's org |
| Any SV user launches via API              | Workflow owner's org |
| Org member launches via web form          | Workflow owner's org |
| Cross-org user launches (SV_USERS access) | Workflow owner's org |

**Rationale:**

1. **Author controls access** — The workflow author decides who can launch. If they open it publicly, they accept the cost.
2. **Simple mental model** — "My workflows use my quota" is easy to understand.
3. **Prevents quota gaming** — Users can't burn someone else's quota by finding public workflows.
4. **Natural upgrade path** — Heavy usage drives upgrades, which is the desired business outcome.

This is documented in detail in ADR-2025-11-28-public-workflow-access.

---

## Modal.com Usage Tracking

### Integration with Modal

```python
# simplevalidations/integrations/modal/tracking.py

from sv_modal.client import ModalClient


class ModalUsageTracker:
    """
    Track Modal compute usage for credit calculation.

    Modal provides runtime metrics via their API. We capture these
    after each job completes to calculate credit consumption.
    """

    def record_job_completion(
        self,
        job_id: str,
        validator: Validator,
        validation_run: ValidationRun,
    ) -> ModalJobRecord:
        """
        Record Modal job completion and calculate credits.
        """
        # Get job metrics from Modal
        client = ModalClient()
        job_metrics = client.get_job_metrics(job_id)

        # Calculate credits
        credits_used = calculate_credits_used(
            runtime_seconds=job_metrics.runtime_seconds,
            validator_weight=validator.compute_weight,
        )

        # Record for billing
        record = ModalJobRecord.objects.create(
            validation_run=validation_run,
            validator=validator,
            job_id=job_id,
            runtime_seconds=job_metrics.runtime_seconds,
            memory_gb=job_metrics.memory_gb,
            cpu_count=job_metrics.cpu_count,
            credits_used=credits_used,
        )

        # Update org credit balance
        org = validation_run.org
        org.subscription.advanced_credits_balance -= credits_used
        org.subscription.save(update_fields=["advanced_credits_balance"])

        # Update usage counter
        counter = org.current_usage_counter
        counter.advanced_credits_used += credits_used
        counter.save(update_fields=["advanced_credits_used"])

        return record
```

### Validator Compute Weights

```python
# simplevalidations/validations/models.py

class Validator(TimeStampedModel):
    # ... existing fields ...

    compute_weight = models.PositiveSmallIntegerField(
        default=ValidatorWeight.LIGHT,
        choices=[
            (ValidatorWeight.LIGHT, _("Light (1x)")),
            (ValidatorWeight.MEDIUM, _("Medium (2x)")),
            (ValidatorWeight.HEAVY, _("Heavy (3x)")),
            (ValidatorWeight.EXTREME, _("Extreme (5x)")),
        ],
        help_text=_(
            "Compute weight multiplier for credit calculation. "
            "Higher weight = more credits consumed per minute."
        ),
    )
```

---

## Implementation Checklist

### Phase 1: Core Billing Infrastructure

- [ ] Create `billing` app with models: `Subscription`, `UsageCounter`, `CreditReservation`
- [ ] Implement `PlanLimits` configuration for all tiers
- [ ] Create Stripe products and prices for plans and credit packs
- [ ] Implement Stripe webhook handler
- [ ] Create checkout flow for plan selection
- [ ] Create checkout flow for credit purchases
- [ ] Implement `BasicWorkflowMeter` for guardrail enforcement
- [ ] Implement `AdvancedWorkflowMeter` for credit consumption
- [ ] Add `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` to settings

### Phase 2: Feature Enforcement

- [ ] Implement `PlanEnforcer` for feature checks
- [ ] Add `require_plan_feature` decorator
- [ ] Enforce workflow creation limits
- [ ] Enforce custom validator limits
- [ ] Enforce payload size limits
- [ ] Gate integrations feature by plan
- [ ] Gate audit logs feature by plan

### Phase 3: Usage Monitoring

- [ ] Create billing dashboard (basic version)
- [ ] Implement `UsageNotificationService`
- [ ] Add usage threshold notifications
- [ ] Track usage by source (API, web, public)
- [ ] Track usage by workflow
- [ ] Extended dashboard for Team plan
- [ ] Comprehensive dashboard for Enterprise

### Phase 4: Modal Integration

- [ ] Add `compute_weight` field to Validator model
- [ ] Implement `ModalUsageTracker`
- [ ] Capture job metrics from Modal API
- [ ] Calculate credits based on runtime and weight
- [ ] Create `ModalJobRecord` for audit trail

### Phase 5: Overage Handling

- [ ] Implement credit pack purchase flow
- [ ] Add auto-purchase option for Team/Enterprise
- [ ] Implement graceful degradation when credits exhausted
- [ ] Clear messaging for upgrade paths

---

## Profit Scenario Analysis

These scenarios validate that the pricing model is financially sound.

### Cost Assumptions

- **Modal effective cost per credit:** ~$0.002 (conservative, includes overhead)
- **Base infrastructure (Heroku, DB, Redis, S3, monitoring):** ~$300/month early stage, scaling to ~$3,000/month at mid-scale
- **Credit pack prices:** Starter $10/100 credits, Team $25/500 credits

### Scenario 1: Early Days (Handful of Customers)

| Metric            | Value                         |
| ----------------- | ----------------------------- |
| Starter orgs      | 3 (using 150 credits/mo each) |
| Team orgs         | 2 (using 800 credits/mo each) |
| Enterprise        | 0                             |
| Overage purchases | None                          |

**Revenue:** 3×$35 + 2×$100 = **$305/mo**  
**Modal cost:** (450 + 1,600) × $0.002 = **$4.10/mo**  
**Infrastructure:** **$300/mo**  
**Gross profit:** $305 - $4 - $300 = **$1/mo** (break-even)

At this stage, you're covering costs while building customer base.

### Scenario 2: Growing (16 Customers, Some Overage)

| Metric       | Value                                    |
| ------------ | ---------------------------------------- |
| Starter orgs | 10 (220 credits/mo, 2 packs each)        |
| Team orgs    | 5 (1,200 credits/mo, 1 pack each)        |
| Enterprise   | 1 @ $1,000/mo (5,500 credits/mo, 1 pack) |

**Revenue:**

- Starter subs: 10 × $35 = $350
- Starter packs: 10 × 2 × $10 = $200
- Team subs: 5 × $100 = $500
- Team packs: 5 × 1 × $25 = $125
- Enterprise sub: 1 × $1,000 = $1,000
- Enterprise packs: 1 × $25 = $25

**Total revenue:** **$2,200/mo**

**Modal cost:** (2,200 + 6,000 + 5,500) × $0.002 = **$27.40/mo**  
**Infrastructure:** **$500/mo** (scaled up a bit)  
**Gross profit:** $2,200 - $27 - $500 = **$1,673/mo (~76% margin)**

### Scenario 3: Mid-Scale Success

| Metric       | Value                                               |
| ------------ | --------------------------------------------------- |
| Starter orgs | 20 (250 credits/mo, 3 packs each)                   |
| Team orgs    | 20 (1,500 credits/mo, 2 packs each)                 |
| Enterprise   | 10 @ $2,000/mo avg (6,000 credits/mo, 2 packs each) |

**Revenue:**

- Starter: 20 × ($35 + 3×$10) = $1,300
- Team: 20 × ($100 + 2×$25) = $3,000
- Enterprise: 10 × ($2,000 + 2×$25) = $20,500

**Total revenue:** **$24,800/mo**

**Modal cost:** 95,000 credits × $0.002 = **$190/mo**  
**Infrastructure:** **$3,000/mo**  
**Gross profit:** $24,800 - $190 - $3,000 = **$21,610/mo (~87% margin)**

### Key Insights

1. **Modal costs are a rounding error.** Even at scale, Modal is <1% of revenue.
2. **Infrastructure is the real fixed cost** in early stages.
3. **Enterprise customers drive profitability.** One $2,000/mo Enterprise = 57 Starter customers.
4. **Credit pack overages are pure margin** after you've covered subscription costs.
5. **The $35→$100 jump is reasonable** (2.9× price for 5× credits + team features).

---

## References

- [Stripe Subscriptions](https://stripe.com/docs/billing/subscriptions/overview) – Subscription lifecycle
- [Stripe Checkout](https://stripe.com/docs/payments/checkout) – Hosted checkout pages
- [Stripe Webhooks](https://stripe.com/docs/webhooks) – Event handling
- [Modal Pricing](https://modal.com/pricing) – Compute cost structure
- [SaaS Pricing Models](https://www.priceintelligently.com/) – Pricing strategy resources
