"""
Billing metering and enforcement classes.

These classes check quotas and enforce plan limits. They are called at
enforcement points (e.g., before launching a workflow, when adding a
team member) to ensure the organization hasn't exceeded their limits.

Usage:
    # Check basic workflow limit before launching
    BasicWorkflowMeter().check_and_increment(org)

    # Check credits before advanced workflow
    meter = AdvancedWorkflowMeter()
    if meter.check_balance(org) < required_credits:
        raise InsufficientCreditsError(...)

    # After advanced workflow completes
    meter.consume_credits(org, credits_used)

    # Check seat limit before adding member
    SeatEnforcer().check_can_add_member(org)
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import transaction

from validibot.billing.constants import SubscriptionStatus
from validibot.billing.models import UsageCounter

if TYPE_CHECKING:
    from validibot.users.models import Organization

logger = logging.getLogger(__name__)

# Constants
DECEMBER = 12


# =============================================================================
# Helper Functions
# =============================================================================


def get_or_create_monthly_counter(org: Organization) -> UsageCounter:
    """
    Get or create a usage counter for the current billing period.

    This function is used by both BasicWorkflowMeter and AdvancedWorkflowMeter
    to track usage within a billing period.

    Args:
        org: The organization to get/create counter for

    Returns:
        UsageCounter for the current billing period
    """
    subscription = org.subscription

    # Use subscription period dates if available, else current month
    if subscription.current_period_start:
        period_start = subscription.current_period_start.date()
        period_end = (
            subscription.current_period_end.date()
            if subscription.current_period_end
            else None
        )
    else:
        # Default to calendar month
        now = datetime.now(tz=UTC)
        period_start = now.replace(day=1).date()
        # Last day of month
        if now.month == DECEMBER:
            period_end = now.replace(year=now.year + 1, month=1, day=1).date()
        else:
            period_end = now.replace(month=now.month + 1, day=1).date()

    counter, _created = UsageCounter.objects.get_or_create(
        org=org,
        period_start=period_start,
        defaults={
            "period_end": period_end,
            "basic_launches": 0,
            "advanced_launches": 0,
            "credits_consumed": 0,
        },
    )

    return counter


# =============================================================================
# Exceptions
# =============================================================================


class BillingError(Exception):
    """Base exception for billing-related errors."""

    def __init__(self, detail: str, code: str = "billing_error"):
        self.detail = detail
        self.code = code
        super().__init__(detail)


class TrialExpiredError(BillingError):
    """Raised when a trial has expired and user must subscribe."""

    def __init__(
        self,
        detail: str = "Your trial has expired. Please subscribe to continue.",
    ):
        super().__init__(detail, code="trial_expired")


class BasicWorkflowLimitError(BillingError):
    """Raised when basic workflow monthly limit is reached."""

    def __init__(
        self,
        detail: str = "Monthly basic workflow limit reached.",
        limit: int | None = None,
    ):
        self.limit = limit
        super().__init__(detail, code="basic_limit_exceeded")


class InsufficientCreditsError(BillingError):
    """Raised when there aren't enough credits for an advanced workflow."""

    def __init__(
        self,
        detail: str = "Insufficient credits for this operation.",
        required: int = 0,
        available: int = 0,
    ):
        self.required = required
        self.available = available
        super().__init__(detail, code="insufficient_credits")


class SeatLimitError(BillingError):
    """Raised when trying to add a member beyond the seat limit."""

    def __init__(
        self,
        detail: str = "Seat limit reached. Upgrade your plan to add more members.",
        limit: int | None = None,
    ):
        self.limit = limit
        super().__init__(detail, code="seat_limit_exceeded")


class SubscriptionInactiveError(BillingError):
    """Raised when the subscription is not in an active state."""

    def __init__(
        self,
        detail: str = "Your subscription is not active.",
        status: str = "",
    ):
        self.status = status
        super().__init__(detail, code="subscription_inactive")


# =============================================================================
# Metering Classes
# =============================================================================


class BasicWorkflowMeter:
    """
    Meter for basic workflow launches.

    Basic workflows have a monthly limit (e.g., 10,000 for Starter).
    This meter checks the limit and increments the counter when allowed.
    """

    def check_and_increment(self, org: Organization) -> None:
        """
        Check if org can launch a basic workflow and increment counter.

        Raises:
            TrialExpiredError: If trial has expired
            SubscriptionInactiveError: If subscription is not active
            BasicWorkflowLimitExceeded: If monthly limit reached
        """
        subscription = org.subscription
        self._check_subscription_status(subscription)

        # Get effective limit (respects Enterprise overrides)
        limit = subscription.get_effective_limit("basic_launches_limit")

        # If no limit (None = unlimited), allow
        if limit is None:
            self._increment_counter(org)
            return

        # Get or create monthly counter
        counter = get_or_create_monthly_counter(org)

        if counter.basic_launches >= limit:
            raise BasicWorkflowLimitError(
                detail=(
                    f"You've reached your monthly limit of {limit:,} basic "
                    "workflow launches. Upgrade your plan for more capacity."
                ),
                limit=limit,
            )

        # Increment counter
        counter.basic_launches += 1
        counter.save(update_fields=["basic_launches"])

        logger.debug(
            "Basic workflow launch for org=%s: %d/%s",
            org.name,
            counter.basic_launches,
            limit,
        )

    def get_usage(self, org: Organization) -> dict:
        """
        Get current basic workflow usage for the billing period.

        Returns:
            dict with 'used', 'limit', and 'remaining' keys
        """
        subscription = org.subscription
        limit = subscription.get_effective_limit("basic_launches_limit")
        counter = get_or_create_monthly_counter(org)

        return {
            "used": counter.basic_launches,
            "limit": limit,
            "remaining": (limit - counter.basic_launches) if limit else None,
            "unlimited": limit is None,
        }

    def _check_subscription_status(self, subscription) -> None:
        """Check if subscription allows operations."""
        if subscription.status == SubscriptionStatus.TRIAL_EXPIRED:
            raise TrialExpiredError

        active_statuses = {
            SubscriptionStatus.TRIALING,
            SubscriptionStatus.ACTIVE,
        }
        if subscription.status not in active_statuses:
            raise SubscriptionInactiveError(
                detail=f"Your subscription is {subscription.get_status_display()}.",
                status=subscription.status,
            )

    def _increment_counter(self, org: Organization) -> None:
        """Increment the basic launches counter (for unlimited plans)."""
        counter = get_or_create_monthly_counter(org)
        counter.basic_launches += 1
        counter.save(update_fields=["basic_launches"])


class AdvancedWorkflowMeter:
    """
    Meter for advanced workflow credits.

    Advanced workflows (e.g., EnergyPlus, FMI) consume credits.
    Credits are deducted from included balance first, then purchased.
    """

    def check_balance(self, org: Organization) -> int:
        """
        Get available credits balance.

        Returns total credits available (included + purchased).
        """
        return org.subscription.total_credits_balance

    def has_credits(self, org: Organization, required: int = 1) -> bool:
        """Check if org has enough credits for an operation."""
        return self.check_balance(org) >= required

    def check_can_launch(self, org: Organization, credits_required: int = 1) -> None:
        """
        Check if org can launch an advanced workflow.

        Raises:
            TrialExpiredError: If trial has expired
            SubscriptionInactiveError: If subscription not active
            InsufficientCreditsError: If not enough credits
        """
        subscription = org.subscription

        # Check subscription status
        if subscription.status == SubscriptionStatus.TRIAL_EXPIRED:
            raise TrialExpiredError

        active_statuses = {
            SubscriptionStatus.TRIALING,
            SubscriptionStatus.ACTIVE,
        }
        if subscription.status not in active_statuses:
            raise SubscriptionInactiveError(
                detail=f"Your subscription is {subscription.get_status_display()}.",
                status=subscription.status,
            )

        # Check credits
        available = self.check_balance(org)
        if available < credits_required:
            raise InsufficientCreditsError(
                detail=(
                    f"This operation requires {credits_required} credits, "
                    f"but you only have {available} available."
                ),
                required=credits_required,
                available=available,
            )

    @transaction.atomic
    def consume_credits(self, org: Organization, amount: int) -> None:
        """
        Consume credits after a workflow completes.

        Deducts from included credits first, then purchased credits.
        Also updates the usage counter.

        Args:
            org: The organization consuming credits
            amount: Number of credits to consume
        """
        if amount <= 0:
            return

        subscription = org.subscription

        # Lock subscription row for update
        subscription = type(subscription).objects.select_for_update().get(
            pk=subscription.pk,
        )

        # Deduct from included first
        if subscription.included_credits_remaining >= amount:
            subscription.included_credits_remaining -= amount
        else:
            # Use all included, then deduct remainder from purchased
            remaining = amount - subscription.included_credits_remaining
            subscription.included_credits_remaining = 0
            subscription.purchased_credits_balance -= remaining

        subscription.save(
            update_fields=[
                "included_credits_remaining",
                "purchased_credits_balance",
            ],
        )

        # Update usage counter
        counter = get_or_create_monthly_counter(org)
        counter.advanced_launches += 1
        counter.credits_consumed += amount
        counter.save(update_fields=["advanced_launches", "credits_consumed"])

        logger.info(
            "Consumed %d credits for org=%s, remaining=%d",
            amount,
            org.name,
            subscription.total_credits_balance,
        )

    def get_usage(self, org: Organization) -> dict:
        """
        Get current credits usage for the billing period.

        Returns:
            dict with credit balances and usage stats
        """
        subscription = org.subscription
        counter = get_or_create_monthly_counter(org)

        return {
            "included_remaining": subscription.included_credits_remaining,
            "purchased_balance": subscription.purchased_credits_balance,
            "total_available": subscription.total_credits_balance,
            "consumed_this_period": counter.credits_consumed,
            "advanced_launches_this_period": counter.advanced_launches,
        }


class SeatEnforcer:
    """
    Enforce seat limits when adding members to an organization.

    Replaces the old OrgQuota.max_seats - now uses Plan/Subscription.
    """

    def check_can_add_member(self, org: Organization) -> None:
        """
        Check if org can add another member.

        Raises:
            SeatLimitExceeded: If org is at max seats
        """
        limit = org.subscription.get_effective_limit("max_seats")

        # None = unlimited (Enterprise)
        if limit is None:
            return

        current_seats = org.membership_set.filter(is_active=True).count()

        if current_seats >= limit:
            raise SeatLimitError(
                detail=(
                    f"Your organization has reached its limit of {limit} seats. "
                    "Upgrade your plan to add more team members."
                ),
                limit=limit,
            )

    def get_seat_usage(self, org: Organization) -> dict:
        """
        Get current seat usage.

        Returns:
            dict with 'used', 'limit', 'remaining', and 'unlimited' keys
        """
        limit = org.subscription.get_effective_limit("max_seats")
        used = org.membership_set.filter(is_active=True).count()

        return {
            "used": used,
            "limit": limit,
            "remaining": (limit - used) if limit else None,
            "unlimited": limit is None,
        }
