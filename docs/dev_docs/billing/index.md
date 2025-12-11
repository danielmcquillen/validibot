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

## Key Flows

### Subscription Signup

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
