# ADR-2025-11-28: Pricing System and Stripe Integration

**Status:** Accepted (2025-12-11) - Implementation in progress  
**Owners:** Platform / Billing / Infrastructure  
**Related ADRs:** [ADR: Invite-Only Free Access and Cross-Org Workflow Sharing](../dev_docs/adr/2025-12-15-free-tier-and-workflow-sharing.md) (workflow guests, sharing attribution)  
**Related docs:** `dev_docs/overview/how_it_works.md`, `billing/models.py`

---

> Note: Seat limits apply to organization memberships. Workflow-level sharing with external users (Workflow Guests) is designed to avoid consuming seats and is documented in [ADR: Invite-Only Free Access and Cross-Org Workflow Sharing](../dev_docs/adr/2025-12-15-free-tier-and-workflow-sharing.md).

## Context

Validibot needs a pricing and billing system that:

1. **Supports tiered plans** – Starter, Team, Enterprise with different feature sets (no free tier; 2-week trial for new orgs).
2. **Meters usage appropriately** – Different metering for different cost structures (see below).
3. **Integrates with Stripe** – Subscription management, credit purchases, invoicing.
4. **Provides usage visibility** – Dashboards and notifications that scale with plan tier.
5. **Enables feature throttling** – Enforce limits based on plan, with clear upgrade paths.

### Two Types of Workflows, Two Metering Models

Workflows are classified as **basic** or **advanced** based on the compute intensity of their validators:

| Workflow Type | Compute Profile                            | Our Cost Model      | How We Meter                       |
| ------------- | ------------------------------------------ | ------------------- | ---------------------------------- |
| **Basic**     | Low-compute validators only                | Negligible per-run  | Hard cap (monthly launch limit)    |
| **Advanced**  | Any high-compute validator (AI, sim, etc.) | Per-compute billing | Consume credits (based on runtime) |

**Basic workflows** use only low-compute validators—schema checks, simple parsing, lightweight rule evaluation. These have negligible per-run cost regardless of where they execute, so we meter by launch count with generous monthly limits. When an organization hits their limit, further basic workflow launches are blocked until the next billing period. Users can configure warning notifications at custom thresholds (e.g., 50%, 80%, 90%).

**Advanced workflows** use at least one high-compute validator—AI models, building simulations, complex analysis. These consume meaningful compute resources (CPU time, memory, sometimes GPU), so we meter by actual resource consumption via credits. There's no launch cap—you can run as many advanced workflows as you have credits for.

This ADR defines the pricing tiers, metering strategy, Stripe integration, and the infrastructure needed to implement it.

---

## Pricing Tiers

### Tier Summary

|             | Starter | Team   | Enterprise       |
| ----------- | ------- | ------ | ---------------- |
| **Price**   | $29/mo  | $99/mo | Contact us       |
| **Seats**   | 2       | 10     | 100              |
| **Support** | Email   | Email  | Priority + Slack |

**Trial:** All new organizations receive a 14-day free trial on Starter plan features. When the trial expires, users must subscribe to continue using the platform.

### Authoring Features

|                              | Starter | Team    | Enterprise |
| ---------------------------- | ------- | ------- | ---------- |
| **Author workflows**         | ✅ Yes  | ✅ Yes  | ✅ Yes     |
| **Use basic validators**     | ✅ Yes  | ✅ Yes  | ✅ Yes     |
| **Use advanced validators**  | ✅ Yes  | ✅ Yes  | ✅ Yes     |
| **Create custom validators** | 10      | 100     | 1,000      |
| **Validation workflows**     | 10      | 100     | 1,000      |
| **Payload size limit**       | ≤ 5 MB  | ≤ 20 MB | ≤ 100 MB   |

### Usage Quotas

|                                             | Starter | Team    | Enterprise |
| ------------------------------------------- | ------- | ------- | ---------- |
| **Basic workflow launches** (per month)     | 10,000  | 100,000 | 1,000,000  |
| **Advanced workflow credits** (included/mo) | 200     | 1,000   | 5,000      |

**Basic workflow limits:** Hard monthly limits. When reached, further basic workflow launches are blocked until the next billing period. Users can configure warning notifications at custom percentage thresholds.

**Advanced workflow credits:** Hard limit based on credits consumed (not launch count). One advanced workflow launch might use 1-50+ credits depending on runtime and validator complexity. Purchase additional credit packs if needed.

### Platform Features

|                        | Starter | Team     | Enterprise         |
| ---------------------- | ------- | -------- | ------------------ |
| **Integrations**       | ❌ No   | ✅ Yes   | ✅ Yes             |
| **Audit logs**         | ❌ No   | ✅ Yes   | ✅ Yes             |
| **Billing dashboard**  | Basic   | Extended | Comprehensive      |
| **Analytics**          | Basic   | Extended | Comprehensive      |
| **Deployment options** | Cloud   | Cloud    | On-prem / regional |

**Audit logs (Team/Enterprise):** Immutable, append-only records of every sensitive action (workflow edits/publishes, validator changes, permission changes, billing events, API launches, credential/integration updates). Each entry captures who, what, when, where (IP/UA), and the before/after payload hashes. Audit logs differ from basic usage tracking: usage counters summarize volume, while audit logs capture actor-level provenance needed for compliance (SOC 2), incident response, and customer attestations. Team users get 90-day retention and CSV export; Enterprise adds 1-year retention, tamper-evident hashing, and export to SIEM (syslog/S3 webhook).

### Overage (Credit Packs)

|                              | Starter          | Team            | Enterprise  |
| ---------------------------- | ---------------- | --------------- | ----------- |
| **Purchase overage credits** | Manual           | Manual/Auto     | Manual/Auto |
| **Pack size**                | 100 credits      | 500 credits     | Negotiated  |
| **Pack price**               | $10 (10¢/credit) | $25 (5¢/credit) | Negotiated  |

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

What this means operationally:

- A “credit” is pegged to a small, predictable slice of Modal compute (1 vCPU + 4 GiB for up to 60s). Longer or heavier jobs cost more credits via the `compute_weight` multiplier.
- Our direct cost per credit is roughly **$0.00132**, so even heavy advanced workloads have negligible marginal cost compared to subscription revenue.
- Credits normalize Modal’s per-second billing into a stable unit that is easy for customers to reason about and easy for us to expose in dashboards and alerts.

**Included credits cost us:**

| Plan       | Included Credits | Our Cost | Subscription Price | Margin on Credits |
| ---------- | ---------------- | -------- | ------------------ | ----------------- |
| Starter    | 200/mo           | ~$0.26   | $25/mo             | Essentially free  |
| Team       | 1,000/mo         | ~$1.32   | $100/mo            | Essentially free  |
| Enterprise | 5,000/mo         | ~$6.60   | $1,000+/mo         | Essentially free  |

**Overage credit pricing:**

| Plan       | Price per Credit | Our Cost | Markup |
| ---------- | ---------------- | -------- | ------ |
| Starter    | $0.10            | $0.00132 | ~75×   |
| Team       | $0.05            | $0.00132 | ~38×   |
| Enterprise | Negotiated       | $0.00132 | Custom |

This pricing is aggressive but fair. Modal costs are a rounding error; the real value is our platform, validators, and workflow orchestration.

“Essentially free” means the included credits cost us well under one dollar per month at current Modal pricing—so they do not meaningfully affect gross margin. We can safely treat included credits as a marketing/convenience feature rather than a material COGS line.

---

## Workflow Classification: Basic vs Advanced

Each workflow is classified based on the compute intensity of its validators.
This classification is recalculated whenever the workflow's validators change.

```python
# validibot/workflows/constants.py

class WorkflowType(models.TextChoices):
    """
    Classification of workflows for metering purposes.

    BASIC: All validators are low-compute (negligible per-run cost).
    ADVANCED: At least one validator is high-compute (metered by credits).
    """
    BASIC = "BASIC", _("Basic")
    ADVANCED = "ADVANCED", _("Advanced")
```

### Validator Compute Tiers

Each validator has a `compute_tier` that indicates its resource intensity:

```python
# validibot/validations/constants.py

class ComputeTier(models.TextChoices):
    """
    Compute intensity classification for validators.

    LOW: Lightweight operations (schema validation, simple parsing, rule checks).
         Negligible cost per run—metered by monthly launch count.

    HIGH: Resource-intensive operations (AI inference, simulations, complex analysis).
          Meaningful cost per run—metered by credit consumption based on runtime.
    """
    LOW = "LOW", _("Low compute")
    HIGH = "HIGH", _("High compute")
```

**Classification rules:**

| Workflow Contains                               | Classification |
| ----------------------------------------------- | -------------- |
| Only LOW compute tier validators                | BASIC          |
| Any HIGH compute tier validator (AI, sim, etc.) | ADVANCED       |

```python
# validibot/workflows/models.py

class Workflow(FeaturedImageMixin, TimeStampedModel):

    @property
    def workflow_type(self) -> WorkflowType:
        """
        Determine workflow type based on validators' compute tiers.

        A workflow is ADVANCED if any of its validators is HIGH compute.
        This is independent of where the validators execute.
        """
        if self.validators.filter(compute_tier=ComputeTier.HIGH).exists():
            return WorkflowType.ADVANCED
        return WorkflowType.BASIC
```

### Default Compute Tiers by Validator Type

| Validator Type         | Default Tier | Rationale                            |
| ---------------------- | ------------ | ------------------------------------ |
| Schema validation      | LOW          | Simple JSON/XML parsing              |
| File format checks     | LOW          | Quick header/magic byte inspection   |
| Regex/pattern matching | LOW          | In-memory string operations          |
| Range/threshold checks | LOW          | Simple numeric comparisons           |
| AI parsing/extraction  | HIGH         | LLM inference, tokenization overhead |
| AI analysis/critique   | HIGH         | LLM inference with complex prompts   |
| EnergyPlus simulation  | HIGH         | Multi-minute building simulation     |
| Custom code validators | Configurable | Depends on what the code does        |

### Execution Target Derivation (heroku vs modal)

`execution_target` is derived, not stored: we infer it from `validation_type` so metering can correctly classify runs without a separate field.

| ValidationType   | execution_target                                           |
| ---------------- | ---------------------------------------------------------- |
| JSON_SCHEMA      | heroku                                                     |
| XML_SCHEMA       | heroku                                                     |
| BASIC            | heroku                                                     |
| CUSTOM_VALIDATOR | heroku (default; override to modal if declared modal-only) |
| ENERGYPLUS       | modal                                                      |
| FMI              | modal                                                      |
| AI_ASSIST        | modal                                                      |

On the `Validator` model we expose:

```python
@property
def execution_target(self) -> str:
    """Derived execution target for metering."""
    match self.validation_type:
        case ValidationType.ENERGYPLUS | ValidationType.FMI | ValidationType.AI_ASSIST:
            return "modal"
        case ValidationType.CUSTOM_VALIDATOR:
            return "modal" if self.runs_on_modal else "heroku"
        case _:
            return "heroku"
```

### Default compute_weight per validation type

We set sensible defaults to avoid under-charging advanced workloads:

| ValidationType    | Default ValidatorWeight | Notes                                     |
| ----------------- | ----------------------- | ----------------------------------------- |
| JSON_SCHEMA       | NORMAL (1×)             | Fast parsing                              |
| XML_SCHEMA        | NORMAL (1×)             | Fast parsing                              |
| BASIC             | NORMAL (1×)             | Simple checks                             |
| CUSTOM_VALIDATOR  | NORMAL (1×)             | Overrideable per validator                |
| AI_ASSIST         | NORMAL (1×)             | MVP: keep advanced at 1× until we profile |
| ENERGYPLUS        | NORMAL (1×)             | MVP: keep advanced at 1× until we profile |
| FMI               | NORMAL (1×)             | MVP: keep advanced at 1× until we profile |
| EXTREME workloads | EXTREME (5×)            | Future: opt-in for GPU/multi-hour jobs    |

MVP stance: all advanced validators ship with `validator_weight = ValidatorWeight.NORMAL` (1×). We retain higher tiers for future tuning once we have real cost data.

---

## Metering Strategy

As explained in the Context section, we use two different metering approaches because our costs differ:

- **Basic workflows** → Hard caps with manual review for edge cases
- **Advanced workflows** → Consume credits based on actual compute time

### Basic Workflow Metering (Hard Limits)

Basic workflows run on our fixed-cost Heroku infrastructure. We enforce hard monthly limits with configurable warning notifications:

```python
# validibot/billing/metering.py

class BasicWorkflowLimitExceeded(Exception):
    """Raised when an org has exhausted their basic workflow quota."""

    def __init__(self, detail: str, code: str = "basic_limit_exceeded"):
        self.detail = detail
        self.code = code
        super().__init__(detail)


class BasicWorkflowMeter:
    """
    Hard-limit metering for basic workflows.

    Hard-limit metering for basic (low-compute) workflows.

    Basic workflows use only low-compute validators with negligible per-run
    cost. We enforce hard monthly limits—when exhausted, further launches
    are blocked until the next billing period. Users can configure warning
    thresholds to get notified before hitting the limit.
    """

    def check_and_increment(self, org: Organization) -> None:
        """
        Check quota and increment launch counter.

        Raises:
            BasicWorkflowLimitExceeded: If the org has hit their monthly limit.
        """
        counter = self._get_or_create_monthly_counter(org)
        limit = self._get_limit(org)

        # All plans have concrete limits (Enterprise: 1M)
        if counter.basic_launches >= limit:
            raise BasicWorkflowLimitExceeded(
                detail=_(
                    "You've reached your monthly limit of %(limit)s basic workflow "
                    "launches. Upgrade your plan or wait until %(reset_date)s."
                ) % {"limit": limit, "reset_date": counter.period_end},
                code="basic_limit_exceeded",
            )

        counter.basic_launches += 1
        counter.save(update_fields=["basic_launches"])

        # Check if any warning thresholds were crossed
        self._check_warning_thresholds(org, counter, limit)

    def _get_limit(self, org: Organization) -> int:
        """Get the basic workflow limit for an org."""
        plan_limits = PLAN_LIMITS[org.subscription.plan]
        return plan_limits.basic_launches_limit

    def _check_warning_thresholds(self, org: Organization, counter, limit: int) -> None:
        """
        Send notifications when user-configured warning thresholds are crossed.

        Users can configure thresholds like [50, 80, 90] to get notified
        at 50%, 80%, and 90% of their limit.
        """

        usage_percent = (counter.basic_launches / limit) * 100
        thresholds = org.subscription.warning_thresholds or [80, 90]

        for threshold in thresholds:
            if self._just_crossed_threshold(counter, limit, threshold):
                self._send_warning_notification(org, threshold, usage_percent)

    def _just_crossed_threshold(self, counter, limit: int, threshold: int) -> bool:
        """Check if this increment just crossed a threshold."""
        current = counter.basic_launches
        previous = current - 1
        threshold_count = int(limit * threshold / 100)
        return previous < threshold_count <= current
```

### Advanced Workflow Metering (Credits)

Credit-based metering tied to actual compute cost:

#### Credit Definition

```
1 credit ≈ up to 60 seconds of 1 vCPU & 4 GiB on Modal
```

#### Credit Calculation

```python
# validibot/billing/credits.py

from enum import IntEnum
from math import ceil


class ValidatorWeight(IntEnum):
    """
    Compute weight multiplier for different validator types.

    Higher weight = more expensive compute profile.
    """
    NORMAL = 1   # Default for all advanced validators (MVP baseline)
    MEDIUM = 2   # Future: heavier AI/inference
    HEAVY = 3    # Future: EnergyPlus/FMI if we re-enable weighting
    EXTREME = 5  # Future: GPU/multi-hour workloads


def calculate_credits_used(
    runtime_seconds: float,
    validator_weight: ValidatorWeight,
) -> int:
    """
    Calculate credits consumed by an advanced workflow run.

    Formula: credits = ceil(runtime_seconds / 60) * validator_weight

    Examples:
        - Normal AI job (45s, weight 1): ceil(45/60) * 1 = 1 credit
        - Medium sim (180s, weight 2): ceil(180/60) * 2 = 6 credits
        - Heavy sim (300s, weight 3): ceil(300/60) * 3 = 15 credits
    """
    minutes = ceil(runtime_seconds / 60)
    return minutes * validator_weight
```

#### Credit Consumption Flow

```python
# validibot/billing/metering.py

class AdvancedWorkflowMeter:
    """
    Credit-based metering for advanced (high-compute) workflows.

    Credits map to actual compute resource consumption, allowing us
    to recover infrastructure costs and price fairly regardless of
    where the compute runs.
    """

    def check_balance(self, org: Organization) -> int:
        """Return remaining credits for the org."""
        return org.subscription.total_credits_balance

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

        # Deduct from org balance (included first, then purchased)
        org = reservation.org
        org.subscription.consume_credits(actual_credits)

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
│   └── $25/month
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
# validibot/billing/models.py

class PricingPlan(models.TextChoices):
    """Available pricing plans (no free tier - 2-week trial instead)."""
    STARTER = "STARTER", _("Starter")
    TEAM = "TEAM", _("Team")
    ENTERPRISE = "ENTERPRISE", _("Enterprise")


class Subscription(TimeStampedModel):
    """
    Organization's subscription to Validibot.

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
        "users.Organization",  # Lives in validibot.users app
        on_delete=models.CASCADE,
        related_name="subscription",
    )

    plan = models.CharField(
        max_length=20,
        choices=PricingPlan.choices,
        default=PricingPlan.STARTER,
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

    # User-configurable warning thresholds (e.g., [50, 80, 90])
    warning_thresholds = ArrayField(
        base_field=models.PositiveIntegerField(),
        default=list,
        blank=True,
        help_text=_("Percentage thresholds for usage warning notifications. Max 5 per org; OWNER-only management."),
    )

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

### Legacy OrgQuota migration

The existing `OrgQuota` model will be deleted. Its fields are replaced by:

- **Plan limits** (`max_submissions_per_day`, etc.) → `Plan` model (lookup table with FK from Subscription)
- **Per-org state** (credit balances) → `Subscription` model
- **Enterprise overrides** → nullable `custom_*` fields on `Subscription`

`Subscription` becomes the single source of truth for plan selection, included/purchased credits, and limits. Access limits via `subscription.plan.<field>` or `subscription.get_effective_limit("<field>")`.

### Subscription Lifecycle (with 2-Week Trial)

- **Creation:** A Subscription record is created when an organization is created, defaulting to Starter plan with TRIALING status and a 14-day trial period.
- **Trial expiry:** When trial expires and no payment method is on file, status becomes TRIAL_EXPIRED. Users are hard-blocked and redirected to a conversion page until they subscribe.
- **Stripe-backed plans:** Subscription holds the Stripe IDs and balances; renewals reset included credits and roll usage counters.
- **No orphaned orgs:** Code should not assume `org.subscription` is missing; creation happens at org creation time.

### Organization Model Extension

The billing system requires a few additions to the existing Organization model
in `validibot.users.models`:

```python
# validibot/users/models.py (additions to existing Organization model)

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
        creating one if it doesn't exist (for trial orgs that
        haven't gone through Stripe checkout yet).
        """
        from validibot.billing.models import UsageCounter
        from validibot.billing.plans import PLAN_LIMITS

        today = timezone.now().date()

        # Try to get existing counter for current period
        counter = self.usage_counters.filter(
            period_start__lte=today,
            period_end__gte=today,
        ).first()

        if counter:
            return counter

        # Create counter for trial orgs (no Stripe subscription yet)
        plan_limits = PLAN_LIMITS[self.subscription.plan]
        period_start = today.replace(day=1)  # First of current month

        # Calculate period end (last day of month)
        next_month = period_start.replace(day=28) + timedelta(days=4)
        period_end = next_month - timedelta(days=next_month.day)

        return UsageCounter.objects.create(
            org=self,
            period_start=period_start,
            period_end=period_end,
            basic_launches_limit=plan_limits.basic_launches_limit or 0,
            advanced_credits_limit=plan_limits.advanced_credits_per_month,
        )
```

### Plan Configuration

```python
# validibot/billing/plans.py

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PlanLimits:
    """Feature limits for a pricing plan."""

    # Authoring limits
    can_author_workflows: bool
    max_workflows: int
    max_custom_validators: int
    max_payload_mb: int

    # Usage limits
    basic_launches_limit: int  # Hard cap blocks at limit
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
    PricingPlan.STARTER: PlanLimits(
        can_author_workflows=True,
        max_workflows=10,
        max_custom_validators=10,
        max_payload_mb=5,
        basic_launches_limit=10_000,
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
        basic_launches_limit=100_000,
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
        max_workflows=1_000,  # 10× Team
        max_custom_validators=1_000,  # 10× Team
        max_payload_mb=100,
        basic_launches_limit=1_000_000,  # 10× Team
        advanced_credits_per_month=5_000,  # Baseline, negotiable
        included_seats=100,  # 10× Team
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
# validibot/billing/webhooks.py

import stripe
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from validibot.billing.models import Subscription
from validibot.billing.services import BillingService


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

### URLs & settings (Stripe and billing)

- **URL patterns** (in `billing/urls.py`, included under `/billing/`):
  - `path("stripe/webhook/", stripe_webhook, name="stripe-webhook")`
  - `path("checkout/<plan>/", PlanCheckoutStartView.as_view(), name="checkout-plan")`
  - `path("credits/checkout/", CreditPackCheckoutStartView.as_view(), name="checkout-credits")`
  - `path("dashboard/", BillingDashboardView.as_view(), name="dashboard")`
  - `path("compute-callback/", ComputeCallbackView.as_view(), name="compute-callback")` # Modal job metrics ingress
- **Settings / environment variables:**
  - `STRIPE_SECRET_KEY`
  - `STRIPE_WEBHOOK_SECRET`
  - `STRIPE_PRICE_ID_STARTER`, `STRIPE_PRICE_ID_TEAM`, `STRIPE_PRICE_ID_ENTERPRISE`
  - `STRIPE_PRICE_ID_CREDITS_STARTER`, `STRIPE_PRICE_ID_CREDITS_TEAM`
  - `STRIPE_PRICE_ID_STARTER_ANNUAL`, `STRIPE_PRICE_ID_TEAM_ANNUAL` (if we add annuals)
  - `STRIPE_TAX_ID` / Stripe Tax settings (if enabled)
- **Recommended libraries (2025):**
  - Official `stripe` Python SDK for Checkout, Billing, and webhooks.
  - Optional: `dj-stripe` if we choose to persist full Stripe objects and leverage its admin tooling; otherwise stick to the lightweight direct-SDK approach above.
- **Webhook best practice:** verify signatures with `STRIPE_WEBHOOK_SECRET`, respond quickly (200), and offload heavy work to background jobs if needed.

### Checkout entry points

- Plan checkout starts from the billing dashboard CTA per plan; we pass `org_id` in metadata and redirect to Stripe Checkout with success/cancel URLs under `/billing/`.
- Credit pack checkout starts from the dashboard “buy credits” CTA; quantity is selected in-app, sent to Checkout with metadata (`org_id`, `type=credit_purchase`, `credits`).
- Success URL returns to `/billing/dashboard/?checkout=success`; cancel returns to `/billing/dashboard/?checkout=cancelled`.

### Stripe customer lifecycle

- Create Stripe Customer on first checkout for an org; store `stripe_customer_id` on Subscription.
- Updates to org name/billing email are pushed to Stripe customer; for multiple orgs per user, we keep one customer per org.
- Payment method updates and invoice history use Stripe Billing Portal (link from dashboard).

### Idempotency and retries

- Webhooks: store processed event IDs to avoid double-processing; Stripe retries on 5xx/timeouts.
- Checkout/session completion: use idempotency keys when creating sessions; webhook handlers must be idempotent and quick.
- Heavy post-processing (e.g., emails, analytics) should go to async jobs.

### Plan changes (upgrade/downgrade) and proration

- Upgrades: immediate plan switch with Stripe proration on; reset included credits to new plan on next invoice.paid.
- Downgrades: effective at period end; on downgrade, enforce lower limits (seats, integrations, audit logs) at the new period. Included credits reset to the downgraded amount; purchased credits stay.
- Seat overages: block new invites when above plan’s included seats; do not auto-bill per-seat in MVP.

### Seat model and limits

- Seats are consumed by active memberships on an organization (one seat per active membership).
- Plan entitlements define the included seat count; enforce via `subscription.get_effective_limit("max_seats")` which looks up the Plan FK (or Enterprise custom override on Subscription).
- When at or above the seat limit, block new invites/membership activations until seats are freed or the plan is upgraded. No per-seat auto-billing in MVP; revisit per-seat add-ons when Stripe plans are stable.

### Payment failures and dunning

- On `invoice.payment_failed`: send in-app warning + email; start grace period (configurable, default 7 days). During grace, allow existing users to view but block new launches of advanced workflows; optionally reduce basic limit.
- On final failure/cancellation: set status to `SUSPENDED`, keep data but block all launches and redirect to billing page until payment is fixed.

### MVP rollout (Heroku/Modal in AU)

- Start AU-only: price in AUD, apply 10% GST, and limit Checkout to AU billing addresses.
- Run Heroku and Modal in AU regions where possible; if any Modal jobs execute outside AU, disclose data egress in ToS/privacy and in the billing dashboard.
- Keep plans simple (e.g., Starter + credit packs); gate Team/Enterprise until tax/FX and seats are ready.
- Use Stripe Billing Portal for payment updates; limit distribution to an allowlist while metering and webhooks stabilize.

### Phase 2: Launching to other markets

- Add USD pricing first (single-currency mode per deployment): introduce explicit USD price IDs for Starter/Team/credits; keep one currency active at a time to avoid mixed baskets.
- Open signups to US/CA/NZ/SG before EU/UK to defer VAT/GDPR complexity; block unsupported countries at signup/checkout.
- Enable Stripe Tax for new regions and capture required tax IDs (VAT/GST) and billing address per jurisdiction; configure inclusive vs exclusive pricing per region.
- Add data residency disclosures: note that Heroku/Modal run in AU (or target region); if we add regional stacks later, document the routing rules.
- Expand plans: un-gate Team/Enterprise, enable seat enforcement, and introduce annual price IDs; keep per-seat billing optional until stable.
- Revisit credit pricing per currency; do not rely on FX conversions—set dedicated price IDs per currency.
- Lift allowlist gradually; monitor dunning, webhook health, and metering accuracy before broadening distribution.

### Data regions

We will support three data regions: AU, US, and EU. MVP is AU-only and we will restrict signups/Checkout to AU until regional stacks are ready.

```python
class DataRegion(models.TextChoices):
    AU = "AU", "Australia"
    US = "US", "United States"
    EU = "EU", "Europe"


class Organization(models.Model):
    ...
    data_region = models.CharField(
        max_length=2,
        choices=DataRegion.choices,
        default=DataRegion.AU,
    )
```

Data residency rules: data stays in the org’s region unless a compute provider (e.g., Modal) for that region is unavailable, in which case we either queue the job or fall back only with explicit disclosure/consent. Each region will have dedicated Heroku/Modal deployments as we expand beyond AU.

### Tax, invoices, receipts

- Use Stripe Tax if enabled; collect billing address/VAT in Checkout.
- Expose invoice PDFs and payment history via Billing Portal; for custom billing (Enterprise), attach invoices manually in Stripe and sync status via webhook.

### Trials and Promos

- **Trial period:** All new organizations receive a 14-day free trial with Starter plan features. After trial expiry, users must subscribe to continue.
- **Promo codes:** Not enabled for MVP. Can enable via Stripe Checkout `discounts` later.

### Checkout Flow

```python
# validibot/billing/services.py

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
    from validibot.users.models import Membership
    from validibot.users.constants import RoleCode

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
            basic_launches_limit=plan_limits.basic_launches_limit or 0,
            advanced_credits_limit=plan_limits.advanced_credits_per_month,
        )
```

---

## Feature Throttling

### Enforcement Points

Features are enforced at multiple points:

```python
# validibot/billing/enforcement.py

from functools import wraps
from django.core.exceptions import PermissionDenied

from validibot.billing.plans import PLAN_LIMITS, PricingPlan


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

### Warning Thresholds

Users can configure up to five custom warning thresholds (per org) to be notified before hitting limits. Only **OWNER** users can view or manage these thresholds in the billing dashboard; other roles can see resulting notifications but cannot edit thresholds. The UI is a simple list with add/edit/delete for percentages plus an optional label.

| Plan       | Default Thresholds | Customizable | Dashboard Features         |
| ---------- | ------------------ | ------------ | -------------------------- |
| Starter    | 50%, 80%, 90%      | Yes          | Basic usage charts         |
| Team       | 50%, 75%, 90%      | Yes          | Extended analytics, trends |
| Enterprise | 50%, 75%, 90%      | Yes + Slack  | Full analytics, exports    |

Warning thresholds are stored per-subscription and can be customized via the billing dashboard (Starter+). Attempts to add more than five thresholds are blocked with inline validation.

When a warning threshold is crossed, we notify in two ways so the owner cannot miss it:

- Send an email to the billing contact (defaults to the org owner; falls back to `billing_email` if present).
- Create an in-app warning notification for the owner in the notifications window, so the banner shows even if the email is skipped.

Each threshold fires once per billing period unless reset by a new period or by updating the threshold value in the dashboard.

### Usage Tracking Model

```python
# validibot/billing/models.py

class UsageCounter(TimeStampedModel):
    """
    Monthly usage counters for an organization.

    A new counter is created at the start of each billing period.
    Historical counters are retained for analytics.

    Billing period alignment: counters align to Stripe subscription periods
    (current_period_start/end from webhook events). Webhook handlers must
    backfill/create counters on `invoice.paid` and `customer.subscription.updated`
    to keep usage in sync with Stripe billing cycles.
    """

    org = models.ForeignKey(
        "users.Organization",  # Lives in validibot.users app
        on_delete=models.CASCADE,
        related_name="usage_counters",
    )

    # Billing period this counter covers
    period_start = models.DateField()
    period_end = models.DateField()

    # Basic workflow usage (hard limit - blocks when exhausted)
    basic_launches = models.IntegerField(default=0)
    basic_launches_limit = models.IntegerField()

    # Advanced workflow usage (hard limit - credits consumed)
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
        """Percentage of limit used (0-100, capped at limit)."""
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
# validibot/billing/notifications.py

from validibot.notifications.models import Notification


# Default warning thresholds by plan (users can customize)
DEFAULT_WARNING_THRESHOLDS = {
    PricingPlan.STARTER: [50, 80, 90],
    PricingPlan.TEAM: [50, 75, 90],
    PricingPlan.ENTERPRISE: [50, 75, 90],
}


class UsageNotificationService:
    """
    Send usage warning notifications based on user-configured thresholds.
    """

    def check_and_notify(self, org: Organization) -> None:
        """
        Check usage levels and send notifications if thresholds crossed.
        """
        counter = org.current_usage_counter
        subscription = org.subscription

        # Use custom thresholds if set, otherwise plan defaults
        thresholds = (
            subscription.warning_thresholds
            or DEFAULT_WARNING_THRESHOLDS.get(subscription.plan, [80, 90])
        )

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
        if threshold >= 90:
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

### Billing Exceptions

```python
class BillingError(Exception):
    """Base class for billing/plan errors with API-friendly fields."""

    def __init__(self, detail: str, code: str, **extra):
        self.detail = detail
        self.code = code
        self.extra = extra
        super().__init__(detail)


class InsufficientCreditsError(BillingError):
    """Raised when advanced credits are insufficient to run a workflow."""

    def __init__(self, detail: str, credits_needed: int):
        super().__init__(detail=detail, code="insufficient_credits", credits_needed=credits_needed)


class QuotaExceededError(BillingError):
    """Raised when a count-based limit (workflows, seats, basic launches) is exceeded."""

    def __init__(self, detail: str, limit: int):
        super().__init__(detail=detail, code="quota_exceeded", limit=limit)


class PayloadTooLargeError(BillingError):
    """Raised when payload size exceeds plan limits."""

    def __init__(self, detail: str, size_mb: float, max_mb: float):
        super().__init__(
            detail=detail,
            code="payload_too_large",
            size_mb=size_mb,
            max_mb=max_mb,
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

## Compute Usage Tracking

### High-Compute Validator Tracking

```python
# validibot/billing/tracking.py


class ComputeUsageTracker:
    """
    Track compute usage for high-compute validators.

    High-compute validators (AI, simulations, etc.) report runtime metrics
    after each job completes. We use these to calculate credit consumption.
    The tracking is infrastructure-agnostic—it works whether the compute
    runs on Modal, a local GPU cluster, or any other provider.
    """

    def record_job_completion(
        self,
        job_id: str,
        validator: Validator,
        validation_run: ValidationRun,
        job_metrics: "JobMetrics",
    ) -> ComputeJobRecord:
        """
        Record high-compute job completion and calculate credits.

        Args:
            job_id: Unique identifier for the compute job.
            validator: The validator that ran.
            validation_run: The parent validation run.
            job_metrics: Runtime metrics from the compute provider.
        """

        # Calculate credits based on runtime and validator weight
        credits_used = calculate_credits_used(
            runtime_seconds=job_metrics.runtime_seconds,
            validator_weight=validator.compute_weight,
        )

        # Record for billing and audit trail
        record = ComputeJobRecord.objects.create(
            validation_run=validation_run,
            validator=validator,
            job_id=job_id,
            runtime_seconds=job_metrics.runtime_seconds,
            memory_gb=job_metrics.memory_gb,
            cpu_count=job_metrics.cpu_count,
            gpu_seconds=job_metrics.gpu_seconds,  # Optional, for GPU workloads
            credits_used=credits_used,
        )

        # Update org credit balance (included first, then purchased)
        org = validation_run.org
        org.subscription.consume_credits(credits_used)

        # Update usage counter
        counter = org.current_usage_counter
        counter.advanced_credits_used += credits_used
        counter.save(update_fields=["advanced_credits_used"])

        return record
```

### Modal metrics integration pattern

Modal does not expose a simple `get_job_metrics(job_id)` polling API. Instead, we will:

1. Pass a `callback_url` (our `/billing/compute-callback/`) when dispatching Modal jobs via `sv_modal`.
2. Modal posts job completion payloads (runtime_seconds, cpu_count, memory_gb, optional gpu_seconds) to that callback.
3. The callback handler calls `ComputeUsageTracker.record_job_completion(...)` with the received metrics.
4. As a fallback for synchronous runs, if `sv_modal` returns metrics in the function result, we call the same tracker directly.

This avoids polling and guarantees we meter every advanced run as soon as Modal reports completion.

**Callback auth:** The `/billing/compute-callback/` endpoint requires an HMAC signature header from `sv_modal` using a shared secret to prevent spoofed usage events. Reject unsigned/invalid requests with 401 and log for audit.

### Validator Model Additions

```python
# validibot/validations/models.py

class Validator(TimeStampedModel):
    # ... existing fields ...

    compute_tier = models.CharField(
        max_length=10,
        choices=ComputeTier.choices,
        default=ComputeTier.LOW,
        help_text=_(
            "Compute intensity tier. LOW = metered by launch count. "
            "HIGH = metered by credit consumption."
        ),
    )

    compute_weight = models.PositiveSmallIntegerField(
        default=ValidatorWeight.NORMAL,
        choices=[
            (ValidatorWeight.NORMAL, _("Normal (1x)")),
            (ValidatorWeight.MEDIUM, _("Medium (2x)")),
            (ValidatorWeight.HEAVY, _("Heavy (3x)")),
            (ValidatorWeight.EXTREME, _("Extreme (5x)")),
        ],
        help_text=_(
            "Credit multiplier for HIGH compute tier validators. "
            "Higher weight = more credits consumed per minute of runtime."
        ),
    )
```

**Note:** `compute_weight` only applies to HIGH tier validators. For LOW tier validators, the weight is ignored since they're metered by launch count, not runtime.

---

## Implementation Checklist

### Phase 1: Core Billing Infrastructure

- [ ] Update `billing` app with models: `Subscription`, `UsageCounter`, `CreditReservation`
- [ ] Implement `PlanLimits` configuration for all tiers
- [ ] Create Stripe products and prices for plans and credit packs
- [ ] Implement Stripe webhook handler
- [ ] Create checkout flow for plan selection
- [ ] Create checkout flow for credit purchases
- [ ] Implement `BasicWorkflowMeter` for hard monthly limit enforcement
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

### Phase 4: Compute Tracking

- [ ] Add `compute_tier` and `compute_weight` fields to Validator model
- [ ] Implement `ComputeUsageTracker` (infrastructure-agnostic)
- [ ] Define `JobMetrics` interface for compute providers
- [ ] Integrate Modal provider (via sv_modal) to report metrics
- [ ] Calculate credits based on runtime and weight
- [ ] Create `ComputeJobRecord` for audit trail

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

**Revenue:** 3×$25 + 2×$100 = **$275/mo**  
**Modal cost:** (450 + 1,600) × $0.002 = **$4.10/mo**  
**Infrastructure:** **$300/mo**  
**Gross profit:** $275 - $4.10 - $300 = **-$29.10/mo** (early-stage loss until volume grows)

At this stage, you're covering costs while building customer base.

### Scenario 2: Growing (16 Customers, Some Overage)

| Metric       | Value                                    |
| ------------ | ---------------------------------------- |
| Starter orgs | 10 (220 credits/mo, 2 packs each)        |
| Team orgs    | 5 (1,200 credits/mo, 1 pack each)        |
| Enterprise   | 1 @ $1,000/mo (5,500 credits/mo, 1 pack) |

**Revenue:**

- Starter subs: 10 × $25 = $250
- Starter packs: 10 × 2 × $10 = $200
- Team subs: 5 × $100 = $500
- Team packs: 5 × 1 × $25 = $125
- Enterprise sub: 1 × $1,000 = $1,000
- Enterprise packs: 1 × $25 = $25

**Total revenue:** **$2,100/mo**

**Modal cost:** (2,200 + 6,000 + 5,500) × $0.002 = **$27.40/mo**  
**Infrastructure:** **$500/mo** (scaled up a bit)  
**Gross profit:** $2,100 - $27.40 - $500 = **$1,572.60/mo (~75% margin)**

### Scenario 3: Mid-Scale Success

| Metric       | Value                                               |
| ------------ | --------------------------------------------------- |
| Starter orgs | 20 (250 credits/mo, 3 packs each)                   |
| Team orgs    | 20 (1,500 credits/mo, 2 packs each)                 |
| Enterprise   | 10 @ $2,000/mo avg (6,000 credits/mo, 2 packs each) |

**Revenue:**

- Starter: 20 × ($25 + 3×$10) = $1,100
- Team: 20 × ($100 + 2×$25) = $3,000
- Enterprise: 10 × ($2,000 + 2×$25) = $20,500

**Total revenue:** **$24,600/mo**

**Modal cost:** 95,000 credits × $0.002 = **$190/mo**  
**Infrastructure:** **$3,000/mo**  
**Gross profit:** $24,600 - $190 - $3,000 = **$21,410/mo (~87% margin)**

### Stress Test: What If Everyone Maxes Out?

Let's assume every customer in Scenario 3 hits their soft caps and buys maximum reasonable overage:

| Tier       | Orgs | Basic Launches (at soft cap) | Advanced Credits (heavy overage) |
| ---------- | ---- | ---------------------------- | -------------------------------- |
| Starter    | 20   | 20 × 10,000 = 200,000        | 20 × (200 + 500) = 14,000        |
| Team       | 20   | 20 × 100,000 = 2,000,000     | 20 × (1,000 + 2,000) = 60,000    |
| Enterprise | 10   | 10 × 500,000 = 5,000,000     | 10 × (5,000 + 3,000) = 80,000    |
| **Total**  |      | **7.2 million/month**        | **154,000 credits/month**        |

**Basic workflow load analysis:**

- 7.2M launches/month ÷ 30 days ÷ 24 hours = ~10,000 launches/hour = ~3/second average
- Assume 500ms average validation time
- Peak load (10× average): ~30 concurrent requests
- **Verdict: Easily handled by standard Heroku setup**

**Advanced workflow (Modal) cost:**

- 154,000 credits × $0.002 = **$308/month**
- Still less than 2% of revenue
- **Verdict: Modal costs remain negligible**

**Revenue at max usage (more credit packs sold):**

- Starter: 20 × ($25 + 5×$10) = $1,500
- Team: 20 × ($100 + 4×$25) = $4,000
- Enterprise: 10 × ($2,000 + 6×$25) = $21,500
- **Total: $27,000/mo** (up from $24,800)

**Conclusion:** Even with everyone at maximum usage, the system handles load comfortably and margins actually improve (more overage revenue, minimal extra cost).

### Key Insights

1. **Modal costs are a rounding error.** Even at scale, Modal is <1% of revenue.
2. **Infrastructure is the real fixed cost** in early stages.
3. **Enterprise customers drive profitability.** One $2,000/mo Enterprise = 57 Starter customers.
4. **Credit pack overages are pure margin** after you've covered subscription costs.
5. **Soft caps for basic workflows work** because Heroku is fixed cost—heavy usage doesn't hurt us.
6. **The system scales gracefully.** Max usage improves margins, not worsens them.

---

## References

- [Stripe Subscriptions](https://stripe.com/docs/billing/subscriptions/overview) – Subscription lifecycle
- [Stripe Checkout](https://stripe.com/docs/payments/checkout) – Hosted checkout pages
- [Stripe Webhooks](https://stripe.com/docs/webhooks) – Event handling
- [Modal Pricing](https://modal.com/pricing) – Compute cost structure
- [SaaS Pricing Models](https://www.priceintelligently.com/) – Pricing strategy resources
