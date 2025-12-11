# Stripe Setup

This guide covers setting up Stripe for both local development and production.

## Test Mode vs Live Mode

Stripe accounts have two completely separate modes:

| Mode | Keys | Purpose |
|------|------|---------|
| **Test** | `pk_test_...`, `sk_test_...` | Development and staging. No real money. |
| **Live** | `pk_live_...`, `sk_live_...` | Production only. Real charges. |

Each mode has its own:

- API keys
- Products and Prices (create separately in each mode)
- Webhook endpoints
- Customer and transaction data

Use test mode for local dev and staging. Use live mode only in production.

## Environment Variables

```bash
# Stripe API Keys
STRIPE_PUBLIC_KEY=pk_test_...      # Publishable key (safe for frontend)
STRIPE_SECRET_KEY=sk_test_...      # Secret key (server-side only)

# Webhook Secret (see sections below for where to get this)
DJSTRIPE_WEBHOOK_SECRET=whsec_...

# dj-stripe Configuration
STRIPE_LIVE_MODE=False             # Set True only for production
DJSTRIPE_FOREIGN_KEY_TO_FIELD=id
```

For local development, add these to `_envs/local/.django`.

---

## Local Development Setup

There are two parts to local Stripe setup:

1. **API Keys** – Authenticate your Django app to call Stripe APIs
2. **Webhook forwarding** – Route Stripe's webhook callbacks to your localhost

### 1. Get Stripe Test Keys

These keys let Django make API calls to Stripe.

1. Create a Stripe account at [stripe.com](https://stripe.com)
2. Ensure you're in **Test mode** (toggle at top-right of Dashboard)
3. Go to **Developers → API Keys**
4. Copy the **Publishable key** (`pk_test_...`) and **Secret key** (`sk_test_...`)

Add to `_envs/local/.django`:

```bash
STRIPE_PUBLIC_KEY=pk_test_your_key_here
STRIPE_SECRET_KEY=sk_test_your_key_here
STRIPE_LIVE_MODE=False
```

### 2. Install Stripe CLI

The Stripe CLI forwards webhooks from Stripe to your local server. Dashboard webhooks require public URLs, so the CLI is the only way to test webhooks locally.

```bash
# macOS
brew install stripe/stripe-cli/stripe

# Or download from https://stripe.com/docs/stripe-cli
```

### 3. Login and Forward Webhooks

```bash
# Authenticate with your Stripe account (opens browser)
stripe login

# Forward webhooks to your local Django server
stripe listen --forward-to localhost:8000/stripe/webhook/
```

The CLI will display:

```
> Ready! Your webhook signing secret is whsec_abc123...
```

**Now copy that secret to your env file before starting Django:**

1. Copy the `whsec_...` value from the CLI output
2. Add to `_envs/local/.django`:
   ```bash
   DJSTRIPE_WEBHOOK_SECRET=whsec_abc123...
   ```
3. Then start Django (it needs this secret to verify incoming webhooks)

!!! warning "Secret changes on restart"
    This secret changes each time you run `stripe listen`. If you restart the CLI, you'll need to update your env file and restart Django.

### 4. Create Test Products and Prices

In the Stripe Dashboard (ensure **Test mode** is active):

1. Go to **Product catalog → Add product**
2. Create products for each plan:

| Product Name | Price | Billing |
|--------------|-------|---------|
| Validibot Starter | $29/month | Recurring |
| Validibot Team | $99/month | Recurring |

3. After creating each product, copy the **Price ID** (`price_...`) from the product details page

4. Update the Plan records in Django:

```python
# In Django shell: uv run python manage.py shell
from validibot.billing.models import Plan

Plan.objects.filter(code="STARTER").update(stripe_price_id="price_xxx")
Plan.objects.filter(code="TEAM").update(stripe_price_id="price_yyy")
```

### 5. Test the Flow

1. Start Django: `source _envs/local/set-env.sh && uv run python manage.py runserver`
2. Start Stripe CLI (in another terminal): `stripe listen --forward-to localhost:8000/stripe/webhook/`
3. Navigate to `/app/billing/` and click "Subscribe"
4. Use test card `4242 4242 4242 4242` (any future expiry, any CVC)
5. Verify the CLI shows the webhook was received
6. Verify your subscription status updated

### Test Card Numbers

| Card | Behavior |
|------|----------|
| `4242 4242 4242 4242` | Succeeds |
| `4000 0000 0000 0002` | Declines |
| `4000 0000 0000 3220` | Requires 3D Secure |

Use any future expiry date and any 3-digit CVC.

---

## Production Setup

### 1. Switch to Live Mode

In Stripe Dashboard, toggle to **Live mode** (top-right).

### 2. Create Live Products

Repeat the product creation process in Live mode. Products and prices don't transfer between modes—you must create them separately.

### 3. Get Live API Keys

Go to **Developers → API Keys** (in Live mode) and copy:

- Publishable key (`pk_live_...`)
- Secret key (`sk_live_...`)

### 4. Create Webhook Endpoint

Unlike local dev, production needs a Dashboard webhook because your server has a public URL.

1. Go to **Developers** and press `w` to open the **Workbench**
2. Click the **Webhooks** tab
3. Click **Add destination**
4. Select these events:
   - `checkout.session.completed`
   - `customer.subscription.trial_will_end`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`
5. Set endpoint URL: `https://your-domain.com/stripe/webhook/`
6. After creating, click the endpoint and copy the **Signing secret** (`whsec_...`)

### 5. Configure Customer Portal

1. Go to **Settings → Billing → Customer portal**
2. Enable:
   - Update payment methods
   - View invoices and billing history
   - Cancel subscription
3. Customize branding to match Validibot

### 6. Set Environment Variables

Add to Secret Manager or deployment config:

```bash
STRIPE_PUBLIC_KEY=pk_live_...
STRIPE_SECRET_KEY=sk_live_...
DJSTRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_LIVE_MODE=True
```

---

## Troubleshooting

### Webhook Signature Verification Failed

**Symptom**: 400 error on webhook endpoint

**Causes**:

1. Wrong `DJSTRIPE_WEBHOOK_SECRET` – ensure it matches (CLI secret for local, Dashboard secret for production)
2. Secret changed – if you restarted `stripe listen`, the secret changed
3. Using Dashboard secret locally – local dev must use the CLI secret

### Checkout Session Not Creating

**Symptom**: Error when clicking Subscribe

**Causes**:

1. Missing `stripe_price_id` on Plan – update Plan with the Price ID from Stripe
2. Invalid API key – verify `STRIPE_SECRET_KEY` is correct for the mode (test/live)
3. Price not active – ensure the Price is active in Stripe Dashboard

### Subscription Not Activating After Payment

**Symptom**: Payment succeeds but subscription stays in TRIALING

**Causes**:

1. Webhook not received – check `stripe listen` output or Dashboard → Webhooks
2. Wrong webhook secret – verify `DJSTRIPE_WEBHOOK_SECRET`
3. Handler error – check Django logs for exceptions

### Customer Portal 404

**Symptom**: "Portal configuration not found"

**Fix**: Enable Customer Portal in Settings → Billing → Customer portal

### dj-stripe API Key Warning

**Symptom**: INFO message about missing API keys in database

This is just informational. Environment variables are sufficient for basic use. Database key storage is only needed for advanced multi-tenant setups.

---

## External Documentation

- [Stripe Checkout](https://stripe.com/docs/payments/checkout)
- [Stripe Customer Portal](https://stripe.com/docs/billing/subscriptions/customer-portal)
- [Stripe CLI](https://stripe.com/docs/stripe-cli)
- [dj-stripe](https://dj-stripe.dev/)
