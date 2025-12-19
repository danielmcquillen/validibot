# Plan Management

This guide covers managing plans, subscriptions, and billing data.

## Seeding Plans

Plans are seeded using a management command that creates the Starter, Team, and Enterprise plans with configuration from the ADR.

### Initial Setup

```bash
# Seed plans (creates if they don't exist)
source set-env.sh && uv run python manage.py seed_plans
```

Output:

```
Created plan: Starter
Created plan: Team
Created plan: Enterprise
Done!

Plan Summary:
------------------------------------------------------------
  Starter: 10,000 launches, 2 seats, $29/mo
  Team: 100,000 launches, 10 seats, $99/mo
  Enterprise: 1,000,000 launches, 100 seats, Contact us
```

### Updating Plans

If you need to update plan limits (e.g., price change, limit adjustment):

```bash
# Update existing plans with latest configuration
source set-env.sh && uv run python manage.py seed_plans --force
```

This preserves the `stripe_price_id` field while updating all other fields.

### Plan Configuration

The plan configuration is in `validibot/billing/management/commands/seed_plans.py`:

```python
PLAN_CONFIG = {
    PlanCode.STARTER: {
        "name": "Starter",
        "description": "Perfect for individuals and small teams...",
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
    # ... Team and Enterprise configs
}
```

---

## Managing Stripe Price IDs

The `seed_plans` command handles both plan creation AND Stripe linking in one step.

After creating products in Stripe with proper metadata, just run:

```bash
uv run python manage.py djstripe_sync_models Price  # Sync from Stripe
uv run python manage.py seed_plans                   # Seed plans + link
```

### How Stripe Linking Works

The command matches Stripe Prices to Plans via product metadata:

| Stripe Product | Metadata | Links To |
|----------------|----------|----------|
| Validibot Starter | `plan_code: STARTER` | Starter plan |
| Validibot Team | `plan_code: TEAM` | Team plan |

### Command Options

```bash
uv run python manage.py seed_plans              # Seed plans + link Stripe
uv run python manage.py seed_plans --force      # Update existing plan limits
uv run python manage.py seed_plans --skip-stripe  # Skip Stripe linking
uv run python manage.py seed_plans --list-stripe  # List available Stripe prices
```

### Via Django Admin

1. Navigate to `/admin/billing/plan/`
2. Click on the plan to edit
3. Copy the Price ID from Stripe Dashboard (`price_xxx`)
4. Paste into `stripe_price_id` field
5. Save

### Via Django Shell

```python
# uv run python manage.py shell

from validibot.billing.models import Plan

# Update Starter
Plan.objects.filter(code="STARTER").update(stripe_price_id="price_1ABC...")

# Update Team
Plan.objects.filter(code="TEAM").update(stripe_price_id="price_1XYZ...")

# Verify
for plan in Plan.objects.all():
    print(f"{plan.name}: {plan.stripe_price_id or 'Not configured'}")
```

!!! warning "Test vs Live Price IDs"
    Test mode and live mode have different Price IDs. Each environment (local, staging, production) needs its own linking.

---

## Managing Subscriptions

### View Subscription Status

```python
# uv run python manage.py shell

from validibot.users.models import Organization

org = Organization.objects.get(name="Acme Corp")
sub = org.subscription

print(f"Plan: {sub.plan.name}")
print(f"Status: {sub.get_status_display()}")
print(f"Stripe Customer: {sub.stripe_customer_id}")
print(f"Stripe Subscription: {sub.stripe_subscription_id}")
print(f"Credits: {sub.total_credits_balance}")
```

### Manually Activate Subscription

If a webhook fails and you need to manually activate:

```python
from validibot.billing.constants import SubscriptionStatus

sub = org.subscription
sub.status = SubscriptionStatus.ACTIVE
sub.stripe_customer_id = "cus_xxx"  # From Stripe Dashboard
sub.stripe_subscription_id = "sub_xxx"  # From Stripe Dashboard
sub.save()
```

### Reset Credits

To manually reset credits (e.g., after manual invoice):

```python
sub = org.subscription
sub.included_credits_remaining = sub.plan.included_credits
sub.save()
```

### Enterprise Custom Limits

Enterprise customers can have custom overrides:

```python
sub = org.subscription
sub.custom_basic_launches_limit = 500_000  # Override plan default
sub.custom_included_credits = 10_000
sub.custom_max_seats = 50
sub.save()
```

These override the Plan defaults when checking limits:

```python
# Returns custom_basic_launches_limit if set, else plan.basic_launches_limit
limit = sub.get_effective_limit("basic_launches_limit")
```

---

## Syncing with Stripe

### Manual Sync

If Stripe and local data get out of sync:

```python
from validibot.billing.services import BillingService

service = BillingService()
service.sync_subscription_from_stripe(org)
```

This pulls the current status from Stripe and updates the local Subscription.

### Viewing Stripe Dashboard Data

1. Go to [Stripe Dashboard](https://dashboard.stripe.com/)
2. Search for customer by email or `cus_xxx` ID
3. View subscription, invoices, and payment history

---

## Usage Tracking

### View Current Usage

```python
from validibot.billing.metering import BasicWorkflowMeter, AdvancedWorkflowMeter

# Basic workflow usage
basic = BasicWorkflowMeter().get_usage(org)
print(f"Basic launches: {basic['used']} / {basic['limit']}")

# Advanced workflow usage
advanced = AdvancedWorkflowMeter().get_usage(org)
print(f"Credits consumed: {advanced['consumed_this_period']}")
print(f"Credits remaining: {advanced['total_available']}")
```

### View Usage Counters Directly

```python
from validibot.billing.models import UsageCounter

# Get all counters for an org
for counter in UsageCounter.objects.filter(org=org).order_by("-period_start"):
    print(f"{counter.period_start}: {counter.basic_launches} basic, {counter.credits_consumed} credits")
```

### Reset Usage Counter (Testing Only)

```python
# For testing - reset counter to zero
counter = UsageCounter.objects.filter(org=org).latest("period_start")
counter.basic_launches = 0
counter.advanced_launches = 0
counter.credits_consumed = 0
counter.save()
```

---

## Common Operations

### Change an Org's Plan

For plan changes, use Stripe Customer Portal (users can self-service) or:

1. Customer cancels current subscription in Stripe
2. Customer subscribes to new plan via Checkout
3. Webhooks handle the transition

Manual override (not recommended):

```python
from validibot.billing.models import Plan

new_plan = Plan.objects.get(code="TEAM")
sub = org.subscription
sub.plan = new_plan
sub.included_credits_remaining = new_plan.included_credits
sub.save()
```

### Cancel a Subscription

Via Stripe (preferred):

```python
import stripe
stripe.api_key = settings.STRIPE_SECRET_KEY

stripe.Subscription.delete(sub.stripe_subscription_id)
# Webhook will update local status
```

Or manually:

```python
sub.status = SubscriptionStatus.CANCELED
sub.save()
```

### Extend a Trial

```python
from django.utils import timezone
from datetime import timedelta

sub = org.subscription
sub.trial_ends_at = timezone.now() + timedelta(days=30)  # 30 more days
sub.status = SubscriptionStatus.TRIALING
sub.save()
```

---

## Debugging

### Check Webhook History

1. Stripe Dashboard → Developers → Webhooks
2. Click your endpoint
3. View recent deliveries and responses

### Check Django Logs

Webhook handlers log to `validibot.billing.webhooks`:

```bash
# In production (GCP)
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"billing"' --limit=50
```

### Common Issues

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Subscription stuck in TRIALING | Webhook not received | Check webhook endpoint, verify secret |
| Credits not resetting | `invoice.paid` webhook failed | Check logs, manually reset credits |
| Wrong plan limits | Using Plan limits instead of custom | Check `custom_*` fields on Subscription |
| Checkout fails | Missing `stripe_price_id` | Update Plan with Price ID from Stripe |
