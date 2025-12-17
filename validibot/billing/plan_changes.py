"""
Plan change service for handling subscription upgrades and downgrades.

This module provides a robust system for changing between plans, handling:
- Free to paid upgrades (via Stripe Checkout)
- Paid to paid changes (via Stripe subscription update with proration)
- Paid to free downgrades (cancel Stripe, move to Free plan)
- Free to free (no-op)

Key design decisions:
- Upgrades are applied immediately with proration
- Downgrades to paid plans are scheduled for end of billing period
- Downgrades to Free are immediate (cancel Stripe subscription)
- All changes are audited via PlanChange model
- Free plan requires no Stripe integration

Mid-cycle behavior:
- Upgrades: Immediate access, prorated charge for remaining period
- Downgrades (paid→paid): Scheduled for period end, no credit issued
- Downgrades (paid→free): Immediate, Stripe subscription canceled

Multiple changes in one cycle:
- Each change is recorded in PlanChange audit log
- Stripe handles proration correctly for multiple changes
- Pending scheduled changes are canceled when new change is made

References:
- https://docs.stripe.com/billing/subscriptions/upgrade-downgrade
- https://docs.stripe.com/billing/subscriptions/prorations
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import stripe
from django.conf import settings
from django.db import transaction

from validibot.billing.constants import PlanCode
from validibot.billing.constants import SubscriptionStatus

if TYPE_CHECKING:
    from datetime import datetime

    from validibot.billing.models import Plan
    from validibot.billing.models import Subscription

logger = logging.getLogger(__name__)


class PlanChangeType(str, Enum):
    """Types of plan changes."""

    UPGRADE = "upgrade"  # Moving to a higher-priced plan
    DOWNGRADE = "downgrade"  # Moving to a lower-priced plan
    LATERAL = "lateral"  # Same price (rare, but possible)


class PlanChangeError(Exception):
    """Base exception for plan change errors."""


class InvalidPlanChangeError(PlanChangeError):
    """Raised when a plan change is not allowed."""


class StripeError(PlanChangeError):
    """Raised when Stripe operation fails."""


@dataclass
class PlanChangeResult:
    """Result of a plan change operation."""

    success: bool
    change_type: PlanChangeType
    old_plan: Plan
    new_plan: Plan
    effective_immediately: bool
    scheduled_at: datetime | None = None
    checkout_url: str | None = None  # For free→paid, redirect to checkout
    message: str = ""
    proration_amount_cents: int | None = None


class PlanChangeService:
    """
    Service for handling plan changes (upgrades and downgrades).

    This service handles all the complexity of plan changes:
    - Determining if change is upgrade or downgrade
    - Handling Free tier specially (no Stripe needed)
    - Managing Stripe subscription updates with proration
    - Scheduling downgrades for end of period
    - Auditing all changes

    Usage:
        service = PlanChangeService()

        # Preview what will happen
        preview = service.preview_change(subscription, new_plan)

        # Execute the change
        result = service.change_plan(subscription, new_plan)

        if result.checkout_url:
            # Free→paid requires checkout redirect
            return redirect(result.checkout_url)
    """

    def __init__(self):
        """Initialize with Stripe API key."""
        stripe.api_key = settings.STRIPE_SECRET_KEY

    def get_change_type(self, old_plan: Plan, new_plan: Plan) -> PlanChangeType:
        """
        Determine if this is an upgrade, downgrade, or lateral move.

        Based on monthly price - higher price = upgrade.
        """
        if new_plan.monthly_price_cents > old_plan.monthly_price_cents:
            return PlanChangeType.UPGRADE
        if new_plan.monthly_price_cents < old_plan.monthly_price_cents:
            return PlanChangeType.DOWNGRADE
        return PlanChangeType.LATERAL

    def can_change_plan(
        self,
        subscription: Subscription,
        new_plan: Plan,
    ) -> tuple[bool, str]:
        """
        Check if a plan change is allowed.

        Returns (allowed, reason) tuple.
        """
        # Can't change to current plan UNLESS we need to purchase it
        # (e.g., trial expired user who hasn't paid yet)
        if subscription.plan.code == new_plan.code:
            # Allow if this is a paid plan without an active Stripe subscription
            # This handles trial-expired users who want to purchase their current plan
            needs_purchase = (
                new_plan.monthly_price_cents > 0
                and not subscription.stripe_subscription_id
            )
            if not needs_purchase:
                return False, "Already on this plan"

        # Can't change to Enterprise without contacting sales
        if new_plan.code == PlanCode.ENTERPRISE:
            return False, "Contact sales for Enterprise"

        # Must be in a valid status to change
        valid_statuses = {
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.TRIALING,
            SubscriptionStatus.TRIAL_EXPIRED,
            SubscriptionStatus.PAST_DUE,
        }
        if subscription.status not in valid_statuses:
            return False, f"Cannot change plan while {subscription.status}"

        # Paid plans require Stripe price ID
        if new_plan.monthly_price_cents > 0 and not new_plan.stripe_price_id:
            return False, "Plan not available for purchase"

        return True, ""

    def preview_change(
        self,
        subscription: Subscription,
        new_plan: Plan,
    ) -> PlanChangeResult:
        """
        Preview what will happen if the plan is changed.

        Does not make any changes - just calculates what would happen.
        """
        old_plan = subscription.plan
        change_type = self.get_change_type(old_plan, new_plan)

        # Check if allowed
        allowed, reason = self.can_change_plan(subscription, new_plan)
        if not allowed:
            return PlanChangeResult(
                success=False,
                change_type=change_type,
                old_plan=old_plan,
                new_plan=new_plan,
                effective_immediately=False,
                message=reason,
            )

        # Determine behavior based on change type and plans
        is_from_free = old_plan.code == PlanCode.FREE
        is_to_free = new_plan.code == PlanCode.FREE
        is_from_paid = old_plan.monthly_price_cents > 0
        is_to_paid = new_plan.monthly_price_cents > 0

        # Free → Paid: Requires checkout
        if is_from_free and is_to_paid:
            return PlanChangeResult(
                success=True,
                change_type=PlanChangeType.UPGRADE,
                old_plan=old_plan,
                new_plan=new_plan,
                effective_immediately=True,
                message="Upgrade requires payment. You'll be redirected to checkout.",
            )

        # Paid → Free: Immediate, cancels Stripe
        if is_from_paid and is_to_free:
            return PlanChangeResult(
                success=True,
                change_type=PlanChangeType.DOWNGRADE,
                old_plan=old_plan,
                new_plan=new_plan,
                effective_immediately=True,
                message=(
                    "Your paid subscription will be canceled immediately. "
                    "You'll move to the Free plan with limited features."
                ),
            )

        # Paid → Paid upgrade: Immediate with proration
        if is_from_paid and is_to_paid and change_type == PlanChangeType.UPGRADE:
            proration = self._preview_proration(subscription, new_plan)
            return PlanChangeResult(
                success=True,
                change_type=change_type,
                old_plan=old_plan,
                new_plan=new_plan,
                effective_immediately=True,
                proration_amount_cents=proration,
                message=(
                    f"Upgrade takes effect immediately. "
                    f"You'll be charged a prorated amount of "
                    f"${proration / 100:.2f} for the remainder of this period."
                    if proration
                    else "Upgrade takes effect immediately."
                ),
            )

        # Paid → Paid downgrade: Scheduled for end of period
        if is_from_paid and is_to_paid and change_type == PlanChangeType.DOWNGRADE:
            scheduled_at = subscription.current_period_end
            if scheduled_at:
                date_str = scheduled_at.strftime("%B %d, %Y")
                scheduled_msg = f"Downgrade scheduled for {date_str}."
            else:
                scheduled_msg = "Downgrade scheduled for end of billing period."
            return PlanChangeResult(
                success=True,
                change_type=change_type,
                old_plan=old_plan,
                new_plan=new_plan,
                effective_immediately=False,
                scheduled_at=scheduled_at,
                message=(
                    f"{scheduled_msg} "
                    "You'll keep your current plan features until then."
                ),
            )

        # Free → Free or other edge cases
        return PlanChangeResult(
            success=True,
            change_type=change_type,
            old_plan=old_plan,
            new_plan=new_plan,
            effective_immediately=True,
            message="Plan change will take effect immediately.",
        )

    def _preview_proration(
        self,
        subscription: Subscription,
        new_plan: Plan,
    ) -> int | None:
        """
        Preview proration amount for upgrade.

        Returns amount in cents, or None if can't calculate.
        """
        if not subscription.stripe_subscription_id:
            return None

        try:
            # Get upcoming invoice preview with the new price
            stripe_sub = stripe.Subscription.retrieve(
                subscription.stripe_subscription_id,
            )

            if not stripe_sub.items.data:
                return None

            item_id = stripe_sub.items.data[0].id

            # Use invoice preview to calculate proration
            invoice = stripe.Invoice.upcoming(
                customer=subscription.stripe_customer_id,
                subscription=subscription.stripe_subscription_id,
                subscription_items=[
                    {
                        "id": item_id,
                        "price": new_plan.stripe_price_id,
                    },
                ],
                subscription_proration_behavior="create_prorations",
            )

            # Sum proration line items
            proration_amount = 0
            for line in invoice.lines.data:
                if line.proration:
                    proration_amount += line.amount

            return max(0, proration_amount)

        except stripe.StripeError as e:
            logger.warning("Failed to preview proration: %s", e)
            return None

    @transaction.atomic
    def change_plan(
        self,
        subscription: Subscription,
        new_plan: Plan,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> PlanChangeResult:
        """
        Execute a plan change.

        For Free→Paid, returns a checkout URL that the user must be redirected to.
        For other changes, applies immediately or schedules as appropriate.

        Args:
            subscription: The subscription to change
            new_plan: The plan to change to
            success_url: For free→paid, URL after successful checkout
            cancel_url: For free→paid, URL if checkout is canceled

        Returns:
            PlanChangeResult with details of what happened
        """
        from validibot.billing.models import PlanChange

        old_plan = subscription.plan
        change_type = self.get_change_type(old_plan, new_plan)

        # Validate
        allowed, reason = self.can_change_plan(subscription, new_plan)
        if not allowed:
            raise InvalidPlanChangeError(reason)

        is_to_free = new_plan.code == PlanCode.FREE
        is_from_paid = old_plan.monthly_price_cents > 0
        is_to_paid = new_plan.monthly_price_cents > 0

        # Check if user needs to go through checkout (no active Stripe subscription)
        needs_checkout = is_to_paid and not subscription.stripe_subscription_id

        result: PlanChangeResult

        # Route to appropriate handler
        if needs_checkout:
            # User needs to pay - redirect to Stripe Checkout
            # This handles: Free→Paid, trial-expired users, or same-plan purchase
            result = self._create_checkout_session(
                subscription,
                new_plan,
                success_url,
                cancel_url,
            )
        elif is_from_paid and is_to_free:
            result = self._downgrade_to_free(subscription, old_plan)
        elif is_from_paid and is_to_paid:
            if change_type == PlanChangeType.UPGRADE:
                result = self._upgrade_paid_to_paid(subscription, new_plan, old_plan)
            else:
                result = self._downgrade_paid_to_paid(
                    subscription,
                    new_plan,
                    old_plan,
                )
        else:
            # Free → Free (shouldn't happen, but handle gracefully)
            result = PlanChangeResult(
                success=True,
                change_type=PlanChangeType.LATERAL,
                old_plan=old_plan,
                new_plan=new_plan,
                effective_immediately=True,
                message="No change needed - already on a free plan.",
            )

        # Audit the change (unless it's a checkout redirect - audited on success)
        if result.success and not result.checkout_url:
            PlanChange.objects.create(
                subscription=subscription,
                old_plan=old_plan,
                new_plan=new_plan,
                change_type=change_type.value,
                effective_immediately=result.effective_immediately,
                scheduled_at=result.scheduled_at,
                proration_amount_cents=result.proration_amount_cents,
            )

        return result

    def _create_checkout_session(
        self,
        subscription: Subscription,
        new_plan: Plan,
        success_url: str | None,
        cancel_url: str | None,
    ) -> PlanChangeResult:
        """
        Create a Stripe Checkout session for purchasing a plan.

        This handles all cases where payment is needed:
        - Free → Paid upgrade
        - Trial expired user purchasing their current plan
        - Trial expired user switching to a different paid plan
        - Any user without an active Stripe subscription going to a paid plan
        """
        from validibot.billing.services import BillingService

        if not success_url or not cancel_url:
            raise InvalidPlanChangeError(
                "success_url and cancel_url required for checkout",
            )

        service = BillingService()

        # Create checkout session (skip trial since they're purchasing)
        checkout_url = service.create_checkout_session(
            org=subscription.org,
            plan=new_plan,
            success_url=success_url,
            cancel_url=cancel_url,
            skip_trial=True,  # No trial for purchases
        )

        return PlanChangeResult(
            success=True,
            change_type=self.get_change_type(subscription.plan, new_plan),
            old_plan=subscription.plan,
            new_plan=new_plan,
            effective_immediately=True,
            checkout_url=checkout_url,
            message="Redirecting to checkout...",
        )

    def _downgrade_to_free(
        self,
        subscription: Subscription,
        old_plan: Plan,
    ) -> PlanChangeResult:
        """
        Handle downgrade from a paid plan to Free.

        Cancels Stripe subscription immediately and moves to Free plan.
        """
        from validibot.billing.models import Plan

        free_plan = Plan.objects.get(code=PlanCode.FREE)

        # Cancel Stripe subscription if exists
        if subscription.stripe_subscription_id:
            try:
                stripe.Subscription.cancel(
                    subscription.stripe_subscription_id,
                    prorate=True,  # Refund unused portion
                )
                logger.info(
                    "Canceled Stripe subscription %s for downgrade to Free",
                    subscription.stripe_subscription_id,
                )
            except stripe.StripeError as e:
                logger.exception("Failed to cancel Stripe subscription")
                raise StripeError(f"Failed to cancel subscription: {e}") from e

        # Update local subscription
        subscription.plan = free_plan
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.stripe_subscription_id = ""
        subscription.current_period_start = None
        subscription.current_period_end = None
        subscription.included_credits_remaining = free_plan.included_credits
        subscription.save()

        return PlanChangeResult(
            success=True,
            change_type=PlanChangeType.DOWNGRADE,
            old_plan=old_plan,
            new_plan=free_plan,
            effective_immediately=True,
            message=(
                "Your subscription has been canceled and you're now on the Free plan. "
            ),
        )

    def _upgrade_paid_to_paid(
        self,
        subscription: Subscription,
        new_plan: Plan,
        old_plan: Plan,
    ) -> PlanChangeResult:
        """
        Handle upgrade from one paid plan to another.

        Applies immediately with proration.
        """
        if not subscription.stripe_subscription_id:
            raise InvalidPlanChangeError(
                "No active Stripe subscription to upgrade",
            )

        try:
            # Get current subscription item
            stripe_sub = stripe.Subscription.retrieve(
                subscription.stripe_subscription_id,
            )

            if not stripe_sub.items.data:
                raise InvalidPlanChangeError("Subscription has no items")

            item_id = stripe_sub.items.data[0].id

            # Update subscription with new price
            # Uses create_prorations by default which adds proration to next invoice
            # For immediate billing, we use always_invoice
            updated_sub = stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                items=[
                    {
                        "id": item_id,
                        "price": new_plan.stripe_price_id,
                    },
                ],
                proration_behavior="always_invoice",  # Bill immediately
                payment_behavior="error_if_incomplete",  # Fail if payment fails
                metadata={
                    "plan_code": new_plan.code,
                    "upgraded_from": old_plan.code,
                },
            )

            # Calculate proration from the latest invoice
            proration_amount = None
            if updated_sub.latest_invoice:
                try:
                    invoice = stripe.Invoice.retrieve(updated_sub.latest_invoice)
                    proration_amount = sum(
                        line.amount
                        for line in invoice.lines.data
                        if line.proration
                    )
                except stripe.StripeError:
                    pass

            # Update local subscription
            subscription.plan = new_plan
            subscription.included_credits_remaining = new_plan.included_credits
            subscription.save()

            logger.info(
                "Upgraded subscription %s from %s to %s",
                subscription.stripe_subscription_id,
                old_plan.code,
                new_plan.code,
            )

            return PlanChangeResult(
                success=True,
                change_type=PlanChangeType.UPGRADE,
                old_plan=old_plan,
                new_plan=new_plan,
                effective_immediately=True,
                proration_amount_cents=proration_amount,
                message=(
                    f"Upgraded to {new_plan.name}! "
                    f"Your new features are available now."
                ),
            )

        except stripe.StripeError as e:
            logger.exception("Stripe error during upgrade")
            raise StripeError(f"Failed to upgrade: {e}") from e

    def _downgrade_paid_to_paid(
        self,
        subscription: Subscription,
        new_plan: Plan,
        old_plan: Plan,
    ) -> PlanChangeResult:
        """
        Handle downgrade from one paid plan to another.

        Schedules the change for end of billing period.
        Uses Stripe subscription schedules for reliability.
        """
        if not subscription.stripe_subscription_id:
            raise InvalidPlanChangeError(
                "No active Stripe subscription to downgrade",
            )

        try:
            # Get current subscription
            stripe_sub = stripe.Subscription.retrieve(
                subscription.stripe_subscription_id,
            )

            # Cancel any existing schedule
            if stripe_sub.schedule:
                with contextlib.suppress(stripe.StripeError):
                    stripe.SubscriptionSchedule.cancel(stripe_sub.schedule)

            # Create a schedule for the downgrade at period end
            schedule = stripe.SubscriptionSchedule.create(
                from_subscription=subscription.stripe_subscription_id,
            )

            # Update schedule to change plan at end of current phase
            stripe.SubscriptionSchedule.modify(
                schedule.id,
                end_behavior="release",
                phases=[
                    {
                        # Current phase - keep current plan until period end
                        "items": [
                            {"price": old_plan.stripe_price_id, "quantity": 1},
                        ],
                        "start_date": schedule.phases[0]["start_date"],
                        "end_date": schedule.phases[0]["end_date"],
                    },
                    {
                        # Next phase - new plan
                        "items": [
                            {"price": new_plan.stripe_price_id, "quantity": 1},
                        ],
                    },
                ],
                metadata={
                    "pending_plan_code": new_plan.code,
                    "downgraded_from": old_plan.code,
                },
            )

            scheduled_at = subscription.current_period_end

            logger.info(
                "Scheduled downgrade for subscription %s from %s to %s at %s",
                subscription.stripe_subscription_id,
                old_plan.code,
                new_plan.code,
                scheduled_at,
            )

            if scheduled_at:
                date_str = scheduled_at.strftime("%B %d, %Y")
            else:
                date_str = "end of billing period"

            return PlanChangeResult(
                success=True,
                change_type=PlanChangeType.DOWNGRADE,
                old_plan=old_plan,
                new_plan=new_plan,
                effective_immediately=False,
                scheduled_at=scheduled_at,
                message=(
                    f"Your downgrade to {new_plan.name} is scheduled for {date_str}. "
                    f"You'll keep your {old_plan.name} features until then."
                ),
            )

        except stripe.StripeError as e:
            logger.exception("Stripe error during downgrade scheduling")
            raise StripeError(f"Failed to schedule downgrade: {e}") from e

    def cancel_scheduled_change(self, subscription: Subscription) -> bool:
        """
        Cancel a scheduled plan change.

        Returns True if a change was canceled, False if none pending.
        """
        if not subscription.stripe_subscription_id:
            return False

        try:
            stripe_sub = stripe.Subscription.retrieve(
                subscription.stripe_subscription_id,
            )

            if not stripe_sub.schedule:
                return False

            stripe.SubscriptionSchedule.cancel(stripe_sub.schedule)

            logger.info(
                "Canceled scheduled change for subscription %s",
                subscription.stripe_subscription_id,
            )
        except stripe.StripeError:
            logger.exception("Failed to cancel scheduled change")
            return False
        else:
            return True

    def get_pending_change(self, subscription: Subscription) -> Plan | None:
        """
        Get the plan that a subscription is scheduled to change to.

        Returns None if no change is pending.
        """
        if not subscription.stripe_subscription_id:
            return None

        try:
            stripe_sub = stripe.Subscription.retrieve(
                subscription.stripe_subscription_id,
                expand=["schedule"],
            )

            if not stripe_sub.schedule:
                return None

            schedule = stripe_sub.schedule
            min_phases_for_pending_change = 2
            if len(schedule.phases) < min_phases_for_pending_change:
                return None

            # Get the price from the next phase
            next_phase = schedule.phases[1]
            if not next_phase.get("items"):
                return None

            next_price_id = next_phase["items"][0].get("price")
            if not next_price_id:
                return None

            # Look up the plan by Stripe price ID
            from validibot.billing.models import Plan

            try:
                return Plan.objects.get(stripe_price_id=next_price_id)
            except Plan.DoesNotExist:
                return None

        except stripe.StripeError:
            return None
