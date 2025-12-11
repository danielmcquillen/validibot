"""
Stripe webhook handlers using dj-stripe signals.

These handlers use Django signals to respond to Stripe webhook events.
dj-stripe 2.9+ provides individual signals for each webhook event type
in djstripe.signals.WEBHOOK_SIGNALS.

Key events handled:
- checkout.session.completed: Provision access after successful checkout
- customer.subscription.trial_will_end: Notify user before trial ends
- customer.subscription.updated: Sync plan changes
- customer.subscription.deleted: Revoke access
- invoice.paid: Reset credits for new billing period
- invoice.payment_failed: Handle failed payments

To test locally:
    stripe listen --forward-to localhost:8000/stripe/webhook/
"""

import logging

from django.dispatch import receiver
from djstripe.signals import WEBHOOK_SIGNALS

from validibot.billing.constants import SubscriptionStatus
from validibot.billing.models import Subscription

logger = logging.getLogger(__name__)


@receiver(WEBHOOK_SIGNALS["checkout.session.completed"])
def handle_checkout_completed(sender, event, **kwargs):
    """
    Provision access after successful Stripe Checkout.

    This is a backup to the checkout success redirect. It handles cases where
    users close their browser after payment but before the redirect completes.

    The client_reference_id on the checkout session contains our org ID.
    """
    session = event.data["object"]
    org_id = session.get("client_reference_id")

    if not org_id:
        logger.warning("checkout.session.completed missing client_reference_id")
        return

    logger.info("checkout.session.completed for org_id=%s", org_id)

    # Update our Subscription model to active status
    subscription = Subscription.objects.filter(org_id=org_id).first()
    if subscription:
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.stripe_customer_id = session.get("customer", "")
        subscription.stripe_subscription_id = session.get("subscription", "")
        subscription.save(
            update_fields=[
                "status",
                "stripe_customer_id",
                "stripe_subscription_id",
            ],
        )
        logger.info(
            "Activated subscription for org_id=%s, plan=%s",
            org_id,
            subscription.plan.code,
        )


@receiver(WEBHOOK_SIGNALS["customer.subscription.trial_will_end"])
def handle_trial_ending(sender, event, **kwargs):
    """
    Handle trial ending notification (fires 3 days before trial ends).

    Use this to notify the user and encourage them to add a payment method.
    """
    stripe_sub = event.data["object"]
    customer_id = stripe_sub.get("customer")

    logger.info(
        "customer.subscription.trial_will_end for customer=%s",
        customer_id,
    )

    # Find subscription by Stripe customer ID
    subscription = Subscription.objects.filter(
        stripe_customer_id=customer_id,
    ).first()

    if subscription:
        # TODO: Send notification email about trial ending
        # This will be implemented when we add email notifications
        logger.info(
            "Trial ending soon for org=%s (org_id=%s)",
            subscription.org.name,
            subscription.org_id,
        )


@receiver(WEBHOOK_SIGNALS["customer.subscription.updated"])
def handle_subscription_updated(sender, event, **kwargs):
    """
    Sync subscription changes from Stripe.

    Handles: plan upgrades/downgrades, status changes, coupon applications.
    dj-stripe auto-syncs the Stripe subscription object, so we just need to
    update our Subscription model if needed.
    """
    stripe_sub = event.data["object"]
    customer_id = stripe_sub.get("customer")
    stripe_status = stripe_sub.get("status")

    logger.info(
        "customer.subscription.updated: customer=%s, status=%s",
        customer_id,
        stripe_status,
    )

    subscription = Subscription.objects.filter(
        stripe_customer_id=customer_id,
    ).first()

    if subscription:
        # Map Stripe status to our status
        status_map = {
            "trialing": SubscriptionStatus.TRIALING,
            "active": SubscriptionStatus.ACTIVE,
            "past_due": SubscriptionStatus.PAST_DUE,
            "canceled": SubscriptionStatus.CANCELED,
            "unpaid": SubscriptionStatus.SUSPENDED,
        }
        new_status = status_map.get(stripe_status)
        if new_status and subscription.status != new_status:
            subscription.status = new_status
            subscription.save(update_fields=["status"])
            logger.info(
                "Updated subscription status for org=%s to %s",
                subscription.org.name,
                new_status,
            )


@receiver(WEBHOOK_SIGNALS["customer.subscription.deleted"])
def handle_subscription_deleted(sender, event, **kwargs):
    """
    Revoke access when subscription ends.

    This fires when a subscription is canceled (either immediately or at
    period end) and the cancellation takes effect.
    """
    stripe_sub = event.data["object"]
    customer_id = stripe_sub.get("customer")

    logger.info(
        "customer.subscription.deleted: customer=%s",
        customer_id,
    )

    subscription = Subscription.objects.filter(
        stripe_customer_id=customer_id,
    ).first()

    if subscription:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.save(update_fields=["status"])
        logger.info(
            "Canceled subscription for org=%s",
            subscription.org.name,
        )


@receiver(WEBHOOK_SIGNALS["invoice.paid"])
def handle_invoice_paid(sender, event, **kwargs):
    """
    Handle successful payment - reset credits for the new billing period.

    When an invoice is paid, we:
    1. Reset included_credits_remaining to the plan's baseline
    2. Ensure subscription status is ACTIVE
    3. Update billing period dates if available
    """
    invoice = event.data["object"]
    customer_id = invoice.get("customer")

    # Only process subscription invoices (not one-time payments)
    if invoice.get("subscription") is None:
        return

    logger.info(
        "invoice.paid: customer=%s, amount=%s",
        customer_id,
        invoice.get("amount_paid"),
    )

    subscription = Subscription.objects.filter(
        stripe_customer_id=customer_id,
    ).select_related("plan").first()

    if subscription:
        # Reset included credits to plan baseline
        subscription.included_credits_remaining = subscription.plan.included_credits
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save(
            update_fields=[
                "included_credits_remaining",
                "status",
            ],
        )
        logger.info(
            "Reset credits for org=%s to %d",
            subscription.org.name,
            subscription.plan.included_credits,
        )


@receiver(WEBHOOK_SIGNALS["invoice.payment_failed"])
def handle_payment_failed(sender, event, **kwargs):
    """
    Handle failed payment - start dunning flow.

    When a payment fails:
    1. Update subscription status to PAST_DUE
    2. Send notification email (future)
    3. Stripe will retry according to its retry schedule
    """
    invoice = event.data["object"]
    customer_id = invoice.get("customer")

    logger.warning(
        "invoice.payment_failed: customer=%s, amount=%s",
        customer_id,
        invoice.get("amount_due"),
    )

    subscription = Subscription.objects.filter(
        stripe_customer_id=customer_id,
    ).first()

    if subscription:
        subscription.status = SubscriptionStatus.PAST_DUE
        subscription.save(update_fields=["status"])
        logger.warning(
            "Payment failed for org=%s, status set to PAST_DUE",
            subscription.org.name,
        )
        # TODO: Send payment failure notification email
