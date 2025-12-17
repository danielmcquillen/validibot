# Plan Change System

This document describes how subscription plan changes (upgrades and downgrades) work in Validibot.

## Overview

The plan change system handles transitions between our four pricing tiers:

| Plan | Price | Stripe Required |
|------|-------|-----------------|
| Free | $0 | No |
| Starter | $29/mo | Yes |
| Team | $99/mo | Yes |
| Enterprise | Custom | Contact Sales |

## Key Design Decisions

### 1. Free Tier Has No Stripe Integration

The Free plan operates entirely without Stripe. Users on Free:

- Have no `stripe_subscription_id`
- Cannot be charged
- Can upgrade to paid plans via Stripe Checkout
- Can access public workflows and workflows they're invited to

### 2. Upgrades Apply Immediately

When upgrading to a higher-priced plan:

- **Effect**: Immediate access to new plan features
- **Billing**: Prorated charge for the remainder of the current period
- **Technical**: Uses `stripe.Subscription.modify()` with `proration_behavior="always_invoice"`

### 3. Downgrades Are Scheduled (Paid→Paid)

When downgrading to a lower-priced paid plan:

- **Effect**: Keeps current plan until end of billing period
- **Billing**: No immediate charge or credit; new price starts next period
- **Technical**: Uses Stripe Subscription Schedules

### 4. Downgrade to Free Is Immediate

When moving from any paid plan to Free:

- **Effect**: Immediate cancellation of Stripe subscription
- **Billing**: Prorated refund for unused time
- **Technical**: Uses `stripe.Subscription.cancel(prorate=True)`

## How It Works

### Plan Change Flow

```
User clicks "Upgrade" or "Downgrade" button
         ↓
    POST /app/billing/change-plan/?plan=TEAM
         ↓
    ChangePlanView validates request
         ↓
    PlanChangeService.change_plan()
         ↓
┌────────────────────────────────────────────┐
│ Free → Paid: Create Checkout Session       │
│   → Redirect to Stripe Checkout            │
├────────────────────────────────────────────┤
│ Paid → Free: Cancel Stripe, update local   │
│   → Immediate, show success message        │
├────────────────────────────────────────────┤
│ Paid → Paid (upgrade): Modify subscription │
│   → Immediate with proration               │
├────────────────────────────────────────────┤
│ Paid → Paid (downgrade): Create schedule   │
│   → Scheduled for end of period            │
└────────────────────────────────────────────┘
         ↓
    Create PlanChange audit record
         ↓
    Redirect to plans page with message
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/app/billing/change-plan/` | POST | Execute a plan change |
| `/app/billing/change-plan/preview/` | GET | Preview what will happen (JSON) |
| `/app/billing/change-plan/cancel/` | POST | Cancel a scheduled change |

### Preview Response

```json
{
  "success": true,
  "change_type": "upgrade",
  "old_plan": "STARTER",
  "new_plan": "TEAM",
  "effective_immediately": true,
  "scheduled_at": null,
  "proration_amount_cents": 5000,
  "message": "Upgrade takes effect immediately. You'll be charged a prorated amount of $50.00 for the remainder of this period."
}
```

## Mid-Cycle Changes

### Proration Calculation

When upgrading mid-cycle, Stripe calculates the proration based on:

1. **Credit**: Unused time on the old plan
2. **Debit**: Cost of new plan for remaining time
3. **Net**: Debit - Credit = Prorated charge

Example: Upgrading from $29 Starter to $99 Team on day 15 of a 30-day period:

- Credit: $29 × (15/30) = $14.50 (unused Starter time)
- Debit: $99 × (15/30) = $49.50 (Team for remaining time)
- Net charge: $49.50 - $14.50 = $35.00

### Multiple Changes in One Cycle

If a user changes plans multiple times in one billing cycle:

1. **Each change is recorded** in the `PlanChange` audit log
2. **Stripe handles proration correctly** - each change is prorated based on actual usage
3. **Pending scheduled changes are canceled** when a new change is made

Example flow:

```
Day 1:  User on Starter ($29)
Day 10: Upgrade to Team ($99) → Charged $XX proration
Day 20: Downgrade to Starter → Scheduled for day 30
Day 25: Change mind, upgrade to Team → Cancels scheduled downgrade, charged proration
```

## Code Structure

### PlanChangeService

The main service class in `validibot/billing/plan_changes.py`:

```python
from validibot.billing.plan_changes import PlanChangeService

service = PlanChangeService()

# Preview a change
preview = service.preview_change(subscription, new_plan)

# Execute a change
result = service.change_plan(
    subscription=subscription,
    new_plan=new_plan,
    success_url="https://...",  # For free→paid
    cancel_url="https://...",   # For free→paid
)

# Cancel a scheduled change
service.cancel_scheduled_change(subscription)
```

### PlanChange Audit Model

All plan changes are recorded for auditing:

```python
class PlanChange(TimeStampedModel):
    subscription = ForeignKey(Subscription)
    old_plan = ForeignKey(Plan)
    new_plan = ForeignKey(Plan)
    change_type = CharField()  # upgrade, downgrade, lateral
    effective_immediately = BooleanField()
    scheduled_at = DateTimeField(null=True)
    proration_amount_cents = IntegerField(null=True)
    notes = TextField()
```

## Error Handling

### InvalidPlanChangeError

Raised when a plan change is not allowed:

- Already on the target plan
- Trying to change to Enterprise (must contact sales)
- Subscription is in invalid status (canceled, suspended)
- Target plan has no Stripe price configured

### StripeError

Raised when a Stripe operation fails:

- Network error
- Payment failure
- Invalid subscription state

## Best Practices

Based on [Stripe documentation](https://docs.stripe.com/billing/subscriptions/upgrade-downgrade):

1. **Always use `error_if_incomplete`** for upgrades to prevent partial failures
2. **Use Subscription Schedules** for downgrades to ensure reliability
3. **Store subscription item IDs** to avoid extra API calls
4. **Preview prorations** before showing them to users
5. **Handle unpaid invoices** - consider disabling prorations if latest invoice is unpaid

## References

- [Stripe: Upgrade and downgrade subscriptions](https://docs.stripe.com/billing/subscriptions/upgrade-downgrade)
- [Stripe: Prorations](https://docs.stripe.com/billing/subscriptions/prorations)
- [Stripe: Subscription schedules](https://docs.stripe.com/billing/subscriptions/subscription-schedules)
