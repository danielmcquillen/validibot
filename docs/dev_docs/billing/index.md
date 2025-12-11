# Billing & Stripe Overview

Validibot uses Stripe for subscription billing, payment processing, and customer management. This section covers how the billing system works and how to set it up.

## Architecture

The billing system consists of several layers:

```
┌─────────────────────────────────────────────────────────────────┐
│                         Stripe                                   │
│  (Checkout, Customer Portal, Subscriptions, Webhooks)           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      dj-stripe                                   │
│  (Django library - syncs Stripe data, handles webhook routing)  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Validibot Billing App                          │
│  Plan, Subscription, UsageCounter, CreditPurchase models        │
│  BillingService, Metering classes, Webhook handlers             │
└─────────────────────────────────────────────────────────────────┘
```

### Key Components

| Component | Purpose |
|-----------|---------|
| **dj-stripe** | Django library that syncs Stripe data and routes webhooks to Django signals |
| **Stripe Checkout** | Hosted payment pages (PCI compliant, no card handling) |
| **Stripe Customer Portal** | Self-service subscription management for customers |
| **Django signals** | Webhook handlers respond to Stripe events |

## Data Model

### Core Models

```
Organization ──1:1── Subscription ──N:1── Plan
                          │
                          ├── CreditPurchase (audit trail)
                          └── UsageCounter (usage tracking)
```

| Model | Purpose |
|-------|---------|
| `Plan` | Lookup table with plan limits (Starter, Team, Enterprise). Single source of truth for plan configuration. |
| `Subscription` | 1:1 with Organization. Tracks status, Stripe IDs, credit balances, and Enterprise overrides. |
| `CreditPurchase` | Audit trail for credit pack purchases. |
| `UsageCounter` | Monthly usage tracking per organization. |

### Subscription Lifecycle

```
                    ┌───────────────┐
      New Org ────► │   TRIALING    │
                    └───────┬───────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
       ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐
       │   ACTIVE    │ │TRIAL_EXPIRED│ │    CANCELED     │
       └──────┬──────┘ └─────────────┘ └─────────────────┘
              │
              ▼
       ┌─────────────┐
       │  PAST_DUE   │ ─────► SUSPENDED ─────► CANCELED
       └─────────────┘
```

## Pricing Plans

| Plan | Basic Launches | Credits | Seats | Price |
|------|----------------|---------|-------|-------|
| **Starter** | 10,000/mo | 200/mo | 2 | $29/mo |
| **Team** | 100,000/mo | 1,000/mo | 10 | $99/mo |
| **Enterprise** | Unlimited | 5,000/mo | Unlimited | Contact us |

### Basic vs Advanced Workflows

- **Basic workflows**: Simple validations that count against the monthly limit. No credits consumed.
- **Advanced workflows**: Compute-intensive operations (EnergyPlus, FMI) that consume credits.

## Plan Model vs dj-stripe Models

The billing system uses two separate data stores for plan information:

| Model | Source | Purpose |
|-------|--------|---------|
| **Plan** (our model) | Django database | App-specific config: limits, features, display order |
| **Product/Price** (dj-stripe) | Synced from Stripe | Payment processing: price IDs, billing intervals |

There is **no foreign key** between them. The connection is a string reference:

```python
# Plan.stripe_price_id = "price_1ABC..." matches Stripe's Price ID
```

This separation is intentional:

- **Plan** defines what customers get (10,000 launches, 2 seats)
- **Price** defines what they pay ($29/month)
- We control Plan; Stripe controls Price

When creating a checkout session, we look up `plan.stripe_price_id` and pass it to Stripe. The enforcement logic only uses our Plan model—it never queries dj-stripe.

## Key Flows

### Pricing → Signup → Billing Flow

The optimal conversion flow preserves plan selection through the signup process:

```
Pricing Page: User clicks "Start free trial" on Team plan
    → /accounts/signup/?plan=TEAM

Signup Page: Shows "You selected Team Plan" context card
    → User creates account
    → Plan stored in session, then on Subscription.intended_plan

Post-Signup Redirect: AccountAdapter.get_signup_redirect_url()
    → /app/billing/?welcome=1&plan=TEAM

Billing Dashboard: Shows welcome message and plan options
    → User sees trial countdown (14 days)
    → Can "Subscribe Now" to skip trial, or continue free trial
    → Persistent trial banner shown on all app pages

Trial Period: Full access for 14 days
    → Trial banner shows days remaining
    → No payment collected yet

Trial Ends: If not converted, redirect to trial-expired page
    → Shows the intended_plan highlighted for easy conversion
```

This flow is handled by:

- [validibot/users/adapters.py](../../../validibot/users/adapters.py) – `AccountAdapter` captures plan and redirects
- [validibot/users/context_processors.py](../../../validibot/users/context_processors.py) – `signup_plan_context()` adds plan to templates
- [validibot/billing/context_processors.py](../../../validibot/billing/context_processors.py) – `trial_banner_context()` provides trial status
- [validibot/templates/account/signup.html](../../../validibot/templates/account/signup.html) – Two-column signup with plan card
- [validibot/templates/billing/partial/trial_banner.html](../../../validibot/templates/billing/partial/trial_banner.html) – Persistent trial banner

The `intended_plan` field on Subscription stores which plan the user selected from pricing, even if they don't immediately subscribe. This helps with:

- Highlighting their original choice on trial-expired page
- Analytics on conversion by plan selection
- Personalized follow-up messaging

### Skip Trial Option

Users can skip the 14-day trial and pay immediately by clicking "Subscribe Now" on the billing dashboard. This passes `skip_trial=1` to the checkout URL, which creates a Stripe subscription without a trial period.

### Direct Subscription Signup

```
User clicks "Subscribe"
    → CheckoutStartView creates Stripe Checkout session
    → User completes payment on Stripe-hosted page
    → Stripe sends webhook (checkout.session.completed)
    → Webhook handler updates Subscription status to ACTIVE
    → User redirected to success page
```

### Monthly Billing Cycle

```
Billing period starts
    → invoice.paid webhook received
    → Reset included_credits_remaining to plan baseline
    → UsageCounter for new period created on first use
```

### Trial Expiration

```
Trial period ends (14 days)
    → Middleware checks trial_ends_at on each request
    → Status set to TRIAL_EXPIRED
    → User redirected to conversion page
    → Must subscribe to continue using the service
```

## Enforcement Points

The billing system enforces limits at these points:

| Enforcement Point | Metering Class | Raises |
|-------------------|----------------|--------|
| Launch basic workflow | `BasicWorkflowMeter` | `BasicWorkflowLimitError` |
| Launch advanced workflow | `AdvancedWorkflowMeter` | `InsufficientCreditsError` |
| Add team member | `SeatEnforcer` | `SeatLimitError` |
| Accept invite | `SeatEnforcer` | `SeatLimitError` |

## Code References

- [validibot/billing/models.py](../../../validibot/billing/models.py) – Plan, Subscription, UsageCounter
- [validibot/billing/services.py](../../../validibot/billing/services.py) – BillingService for Stripe operations
- [validibot/billing/webhooks.py](../../../validibot/billing/webhooks.py) – Webhook signal handlers
- [validibot/billing/metering.py](../../../validibot/billing/metering.py) – Enforcement classes
- [validibot/billing/views.py](../../../validibot/billing/views.py) – Billing dashboard and checkout views
- [validibot/billing/middleware.py](../../../validibot/billing/middleware.py) – Trial expiry redirect

## Documentation

- [Setup Guide](setup.md) – Local and production Stripe setup
- [Testing](testing.md) – How to test billing flows
- [Management](management.md) – Managing plans and subscriptions
