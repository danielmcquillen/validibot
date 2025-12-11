# Testing Billing Flows

This guide covers how to test the billing system both manually and with automated tests.

## Manual Testing with Stripe CLI

### Prerequisites

1. Stripe CLI installed and authenticated
2. Django running locally
3. Webhook forwarding active

```bash
# Terminal 1: Start Django
source _envs/local/set-env.sh && uv run python manage.py runserver

# Terminal 2: Forward webhooks
stripe listen --forward-to localhost:8000/stripe/webhook/
```

### Test Cards

| Card Number | Behavior |
|-------------|----------|
| `4242 4242 4242 4242` | Succeeds |
| `4000 0000 0000 0002` | Declines |
| `4000 0000 0000 3220` | Requires 3D Secure |
| `4000 0000 0000 9995` | Insufficient funds |

Use any future expiry date and any 3-digit CVC.

### Testing Checkout Flow

1. Log in and navigate to `/app/billing/`
2. Click "Upgrade" on a plan with a `stripe_price_id`
3. Complete checkout with test card `4242 4242 4242 4242`
4. Verify:
   - Stripe CLI shows `checkout.session.completed` webhook
   - Subscription status changes to `ACTIVE`
   - You're redirected to success page

### Testing Webhook Events with CLI Triggers

The Stripe CLI can simulate webhook events without going through the full flow:

```bash
# Simulate successful checkout
stripe trigger checkout.session.completed

# Simulate payment failure
stripe trigger invoice.payment_failed

# Simulate subscription cancellation
stripe trigger customer.subscription.deleted

# Simulate trial ending (3 days before)
stripe trigger customer.subscription.trial_will_end
```

!!! note "CLI triggers vs real events"
    CLI-triggered events have synthetic data that won't match your database. Use them to verify webhook handlers work, but test the full flow for end-to-end validation.

### Testing Trial Expiration

1. Create an org with a subscription in TRIALING status
2. Set `trial_ends_at` to a past date:
   ```python
   from django.utils import timezone
   sub = org.subscription
   sub.trial_ends_at = timezone.now() - timezone.timedelta(days=1)
   sub.save()
   ```
3. Try to access any `/app/` page
4. Verify redirect to `/app/billing/trial-expired/`

### Testing Billing Limits

#### Basic Workflow Limit

```python
from validibot.billing.metering import BasicWorkflowMeter, BasicWorkflowLimitError

meter = BasicWorkflowMeter()

# Check current usage
usage = meter.get_usage(org)
print(f"Used: {usage['used']} / {usage['limit']}")

# Try to increment (will raise if at limit)
try:
    meter.check_and_increment(org)
except BasicWorkflowLimitError as e:
    print(f"Limit reached: {e.detail}")
```

#### Credits Check

```python
from validibot.billing.metering import AdvancedWorkflowMeter, InsufficientCreditsError

meter = AdvancedWorkflowMeter()

# Check balance
balance = meter.check_balance(org)
print(f"Credits available: {balance}")

# Try to launch (will raise if insufficient)
try:
    meter.check_can_launch(org, credits_required=10)
except InsufficientCreditsError as e:
    print(f"Insufficient: {e.detail}")
```

#### Seat Limit

```python
from validibot.billing.metering import SeatEnforcer, SeatLimitError

enforcer = SeatEnforcer()

# Check current seats
usage = enforcer.get_seat_usage(org)
print(f"Seats: {usage['used']} / {usage['limit']}")

# Try to add member (will raise if at limit)
try:
    enforcer.check_can_add_member(org)
except SeatLimitError as e:
    print(f"Seat limit: {e.detail}")
```

---

## Automated Tests

### Test File Location

Billing tests are in `validibot/billing/tests/`:

```
validibot/billing/tests/
├── __init__.py
├── test_metering.py      # Metering class tests
├── test_webhooks.py      # Webhook handler tests
├── test_services.py      # BillingService tests
└── test_views.py         # View tests
```

### Running Billing Tests

```bash
# Run all billing tests
DJANGO_SETTINGS_MODULE=config.settings.local DATABASE_URL="sqlite:///db.sqlite3" \
  uv run pytest validibot/billing/tests/ -v

# Run specific test file
DJANGO_SETTINGS_MODULE=config.settings.local DATABASE_URL="sqlite:///db.sqlite3" \
  uv run pytest validibot/billing/tests/test_metering.py -v
```

### Metering Tests Example

```python
# validibot/billing/tests/test_metering.py

import pytest
from django.utils import timezone

from validibot.billing.constants import PlanCode, SubscriptionStatus
from validibot.billing.metering import (
    BasicWorkflowMeter,
    BasicWorkflowLimitError,
    SeatEnforcer,
    SeatLimitError,
)
from validibot.billing.models import Plan, Subscription


@pytest.fixture
def starter_org(db):
    """Create an org on the Starter plan."""
    from validibot.users.models import Organization

    org = Organization.objects.create(name="Test Org")
    plan = Plan.objects.get(code=PlanCode.STARTER)
    Subscription.objects.create(
        org=org,
        plan=plan,
        status=SubscriptionStatus.ACTIVE,
    )
    return org


class TestBasicWorkflowMeter:
    def test_increments_counter(self, starter_org):
        meter = BasicWorkflowMeter()
        usage_before = meter.get_usage(starter_org)

        meter.check_and_increment(starter_org)

        usage_after = meter.get_usage(starter_org)
        assert usage_after["used"] == usage_before["used"] + 1

    def test_raises_at_limit(self, starter_org):
        meter = BasicWorkflowMeter()

        # Set counter to limit
        counter = meter._get_or_create_monthly_counter(starter_org)
        counter.basic_launches = starter_org.subscription.plan.basic_launches_limit
        counter.save()

        with pytest.raises(BasicWorkflowLimitError):
            meter.check_and_increment(starter_org)


class TestSeatEnforcer:
    def test_allows_within_limit(self, starter_org):
        enforcer = SeatEnforcer()
        # Starter has 2 seats, org starts with 0 members
        enforcer.check_can_add_member(starter_org)  # Should not raise

    def test_raises_at_limit(self, starter_org):
        enforcer = SeatEnforcer()
        # Add members up to limit
        # ... (add 2 members)

        with pytest.raises(SeatLimitError):
            enforcer.check_can_add_member(starter_org)
```

### Webhook Tests Example

```python
# validibot/billing/tests/test_webhooks.py

import pytest
from unittest.mock import MagicMock

from validibot.billing.constants import SubscriptionStatus
from validibot.billing.webhooks import handle_checkout_completed


class TestCheckoutCompletedWebhook:
    def test_activates_subscription(self, db, starter_org):
        # Set up subscription in trial
        sub = starter_org.subscription
        sub.status = SubscriptionStatus.TRIALING
        sub.save()

        # Create mock event
        event = MagicMock()
        event.data = {
            "object": {
                "client_reference_id": str(starter_org.id),
                "customer": "cus_test123",
                "subscription": "sub_test456",
            }
        }

        handle_checkout_completed(sender=None, event=event)

        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.stripe_customer_id == "cus_test123"
        assert sub.stripe_subscription_id == "sub_test456"
```

---

## Testing Checklist

Use this checklist when testing billing changes:

### Subscription Flow
- [ ] New org starts with TRIALING status
- [ ] Trial countdown shows correct days remaining
- [ ] Checkout redirects to Stripe
- [ ] Successful payment activates subscription
- [ ] Cancel returns to billing page without changes

### Webhook Handling
- [ ] `checkout.session.completed` activates subscription
- [ ] `invoice.paid` resets monthly credits
- [ ] `invoice.payment_failed` sets PAST_DUE status
- [ ] `customer.subscription.deleted` sets CANCELED status
- [ ] Handlers are idempotent (can process same event twice)

### Enforcement
- [ ] Basic workflow limit blocks at threshold
- [ ] Credit check blocks when insufficient
- [ ] Seat limit blocks new invites
- [ ] Trial expired redirects to conversion page
- [ ] Error messages are user-friendly

### Customer Portal
- [ ] Portal link works for paying customers
- [ ] Users can update payment method
- [ ] Users can view invoices
- [ ] Users can cancel subscription
