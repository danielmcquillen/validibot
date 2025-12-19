"""
Billing service for Stripe operations.

This service provides a clean interface for:
- Creating Stripe checkout sessions (subscription signup)
- Managing Stripe Customer Portal (self-service management)
- Getting or creating Stripe customers

We use Stripe Checkout (not custom payment forms) for PCI compliance.
Stripe Customer Portal handles self-service management.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import stripe
from django.conf import settings

if TYPE_CHECKING:
    from validibot.billing.models import Plan
    from validibot.users.models import Organization

logger = logging.getLogger(__name__)


class BillingService:
    """
    Service for Stripe billing operations.

    Uses Stripe Checkout for payments (not custom forms) - simplifies PCI compliance.
    Uses Stripe Customer Portal for self-service management.

    Usage:
        service = BillingService()
        checkout_url = service.create_checkout_session(
            org=org,
            plan=plan,
            success_url="https://example.com/billing/success/",
            cancel_url="https://example.com/billing/",
        )
    """

    def __init__(self):
        """Initialize with Stripe API key from settings."""
        stripe.api_key = settings.STRIPE_SECRET_KEY

    def get_or_create_stripe_customer(
        self,
        org: Organization,
    ) -> str:
        """
        Get existing Stripe customer or create a new one.

        Returns the Stripe customer ID (cus_xxx).
        """
        subscription = org.subscription

        # Return existing customer if we have one
        if subscription.stripe_customer_id:
            return subscription.stripe_customer_id

        # Create new Stripe customer
        billing_email = self._get_billing_email(org)

        customer = stripe.Customer.create(
            email=billing_email,
            name=org.name,
            metadata={
                "org_id": str(org.id),
                "org_name": org.name,
            },
        )

        # Save customer ID to subscription
        subscription.stripe_customer_id = customer.id
        subscription.save(update_fields=["stripe_customer_id"])

        logger.info(
            "Created Stripe customer %s for org %s",
            customer.id,
            org.name,
        )

        return customer.id

    def create_checkout_session(
        self,
        org: Organization,
        plan: Plan,
        success_url: str,
        cancel_url: str,
        *,
        skip_trial: bool = False,
    ) -> str:
        """
        Create a Stripe Checkout session for subscription signup.

        Returns the checkout session URL to redirect the user to.

        Args:
            org: The organization subscribing
            plan: The plan to subscribe to
            success_url: URL to redirect to after successful payment
            cancel_url: URL to redirect to if user cancels
            skip_trial: If True, start subscription immediately without trial

        Raises:
            ValueError: If plan has no stripe_price_id configured
        """
        if not plan.stripe_price_id:
            msg = f"Plan {plan.code} has no stripe_price_id configured"
            raise ValueError(msg)

        customer_id = self.get_or_create_stripe_customer(org)

        # Build subscription_data based on whether user wants trial or not
        subscription_data = {
            "metadata": {
                "org_id": str(org.id),
                "plan_code": plan.code,
            },
        }

        if not skip_trial:
            # Default: 14-day free trial
            subscription_data["trial_period_days"] = 14

        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[
                {
                    "price": plan.stripe_price_id,
                    "quantity": 1,
                },
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            # client_reference_id is critical - webhook handler uses this
            # to associate the checkout with our org
            client_reference_id=str(org.id),
            metadata={
                "org_id": str(org.id),
                "plan_code": plan.code,
                "skip_trial": "1" if skip_trial else "0",
            },
            subscription_data=subscription_data,
            # Allow promotion codes
            allow_promotion_codes=True,
            # Collect billing address for tax purposes
            billing_address_collection="auto",
        )

        logger.info(
            "Created checkout session %s for org %s, plan %s, skip_trial=%s",
            session.id,
            org.name,
            plan.code,
            skip_trial,
        )

        return session.url

    def get_customer_portal_url(
        self,
        org: Organization,
        return_url: str,
    ) -> str:
        """
        Get a Stripe Customer Portal URL for self-service management.

        The portal allows customers to:
        - Update payment methods
        - View invoices and payment history
        - Cancel or modify their subscription
        - Download receipts

        Args:
            org: The organization to manage
            return_url: URL to return to after portal session

        Returns:
            URL to redirect user to
        """
        customer_id = self.get_or_create_stripe_customer(org)

        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )

        logger.info(
            "Created portal session for org %s",
            org.name,
        )

        return session.url

    def _get_billing_email(self, org: Organization) -> str:
        """
        Get the billing contact email for an organization.

        Returns the owner's email, or falls back to first admin.
        """
        from validibot.users.constants import RoleCode
        from validibot.users.models import Membership

        # Try to find owner first
        owner = (
            Membership.objects.filter(
                org=org,
                is_active=True,
                membership_roles__role__code=RoleCode.OWNER,
            )
            .select_related("user")
            .first()
        )

        if owner:
            return owner.user.email

        # Fallback to any admin
        admin = (
            Membership.objects.filter(
                org=org,
                is_active=True,
                membership_roles__role__code=RoleCode.ADMIN,
            )
            .select_related("user")
            .first()
        )

        if admin:
            return admin.user.email

        # Last resort: any active member
        member = (
            Membership.objects.filter(
                org=org,
                is_active=True,
            )
            .select_related("user")
            .first()
        )

        return member.user.email if member else ""

    def sync_subscription_from_stripe(
        self,
        org: Organization,
    ) -> None:
        """
        Sync subscription state from Stripe.

        Useful for manual reconciliation or after webhook failures.
        """
        from validibot.billing.constants import SubscriptionStatus

        subscription = org.subscription

        if not subscription.stripe_subscription_id:
            logger.warning(
                "Cannot sync: org %s has no stripe_subscription_id",
                org.name,
            )
            return

        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
        )

        # Map Stripe status to our status
        status_map = {
            "trialing": SubscriptionStatus.TRIALING,
            "active": SubscriptionStatus.ACTIVE,
            "past_due": SubscriptionStatus.PAST_DUE,
            "canceled": SubscriptionStatus.CANCELED,
            "unpaid": SubscriptionStatus.SUSPENDED,
        }

        new_status = status_map.get(stripe_sub.status)
        if new_status:
            subscription.status = new_status
            subscription.save(update_fields=["status"])
            logger.info(
                "Synced subscription for org %s: status=%s",
                org.name,
                new_status,
            )
