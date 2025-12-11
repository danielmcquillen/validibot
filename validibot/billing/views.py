"""
Billing views for subscription management and billing dashboard.

Views in this module:
- BillingDashboardView: Main billing overview page
- TrialExpiredView: Conversion page for expired trials
- CheckoutStartView: Redirect to Stripe Checkout
- CheckoutSuccessView: Handle successful checkout return
- CustomerPortalView: Redirect to Stripe Customer Portal
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import RedirectView
from django.views.generic import TemplateView

from validibot.billing.constants import SubscriptionStatus
from validibot.billing.metering import BasicWorkflowMeter
from validibot.billing.models import Plan
from validibot.billing.models import Subscription
from validibot.users.mixins import OrgMixin

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http import HttpResponse

    from validibot.users.models import Organization

logger = logging.getLogger(__name__)


def get_or_create_subscription(org: Organization) -> Subscription:
    """
    Get or create a subscription for an organization.

    For organizations created before the billing system, this creates
    a default subscription on the Starter plan with a 14-day trial.
    """
    from datetime import timedelta

    try:
        return org.subscription
    except Subscription.DoesNotExist:
        # Create default subscription for legacy organizations
        starter_plan = Plan.objects.get(code="STARTER")
        now = timezone.now()

        subscription = Subscription.objects.create(
            org=org,
            plan=starter_plan,
            status=SubscriptionStatus.TRIALING,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=14),
            included_credits_remaining=starter_plan.included_credits,
        )

        logger.info(
            "Created default subscription for org %s (legacy)",
            org.name,
        )

        return subscription


class BillingDashboardView(LoginRequiredMixin, OrgMixin, TemplateView):
    """
    Main billing dashboard showing subscription status and usage.

    Shows:
    - Current plan and subscription status
    - Trial countdown (if applicable)
    - Usage summary (basic launches, credits)
    - Upgrade options
    - Welcome message for new signups
    """

    template_name = "billing/dashboard.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        subscription = get_or_create_subscription(self.org)
        plan = subscription.plan

        # Calculate trial info
        is_trial = subscription.status == SubscriptionStatus.TRIALING
        trial_days_remaining = 0
        if is_trial and subscription.trial_ends_at:
            delta = subscription.trial_ends_at - timezone.now()
            trial_days_remaining = max(0, delta.days)

        # Get usage stats
        meter = BasicWorkflowMeter()
        usage = meter.get_usage(self.org)

        # Check for welcome flag (new signup from pricing page)
        is_welcome = self.request.GET.get("welcome") == "1"
        selected_plan_code = self.request.GET.get("plan")

        context.update({
            "org": self.org,
            "subscription": subscription,
            "plan": plan,
            "is_trial": is_trial,
            "trial_days_remaining": trial_days_remaining,
            "basic_usage": usage,
            "credits_balance": subscription.total_credits_balance,
            "all_plans": Plan.objects.all(),
            "can_upgrade": plan.code != "ENTERPRISE",
            "stripe_public_key": settings.STRIPE_PUBLIC_KEY,
            "is_welcome": is_welcome,
            "selected_plan_code": selected_plan_code,
        })

        return context


class PlansView(LoginRequiredMixin, OrgMixin, TemplateView):
    """
    Plan comparison page for upgrading subscriptions.

    Shows all available plans with features and pricing,
    allowing users to select and subscribe.
    """

    template_name = "billing/plans.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        subscription = get_or_create_subscription(self.org)
        plan = subscription.plan

        # Calculate trial info
        is_trial = subscription.status == SubscriptionStatus.TRIALING
        trial_days_remaining = 0
        if is_trial and subscription.trial_ends_at:
            delta = subscription.trial_ends_at - timezone.now()
            trial_days_remaining = max(0, delta.days)

        context.update({
            "org": self.org,
            "subscription": subscription,
            "plan": plan,
            "is_trial": is_trial,
            "trial_days_remaining": trial_days_remaining,
            "all_plans": Plan.objects.all(),
        })

        return context


class TrialExpiredView(LoginRequiredMixin, OrgMixin, TemplateView):
    """
    Conversion page shown when trial has expired.

    Shows:
    - Trial expiration message
    - Usage summary from trial period
    - Plan comparison
    - Subscribe CTA
    """

    template_name = "billing/trial_expired.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        subscription = get_or_create_subscription(self.org)

        # Get usage from trial period
        meter = BasicWorkflowMeter()
        usage = meter.get_usage(self.org)

        context.update({
            "org": self.org,
            "subscription": subscription,
            "usage": usage,
            "all_plans": Plan.objects.all(),
            "stripe_public_key": settings.STRIPE_PUBLIC_KEY,
        })

        return context


class CheckoutStartView(LoginRequiredMixin, OrgMixin, RedirectView):
    """
    Start Stripe Checkout session for subscription signup.

    Creates a checkout session and redirects user to Stripe.

    Query params:
    - plan: Plan code (required)
    - skip_trial: If "1", create subscription without trial period
    """

    permanent = False

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        from validibot.billing.services import BillingService

        plan_code = request.GET.get("plan", "STARTER")
        skip_trial = request.GET.get("skip_trial") == "1"

        try:
            plan = Plan.objects.get(code=plan_code)
        except Plan.DoesNotExist:
            logger.warning("Invalid plan code: %s", plan_code)
            messages.error(
                request,
                _("The selected plan is not available. Please choose another plan."),
            )
            return redirect("billing:plans")

        if not plan.stripe_price_id:
            logger.error("Plan %s has no stripe_price_id", plan.code)
            messages.error(
                request,
                _(
                    "This plan is not yet available for purchase. "
                    "Please contact support or try another plan."
                ),
            )
            return redirect("billing:plans")

        # Check Stripe keys are configured
        if not settings.STRIPE_SECRET_KEY:
            logger.error("STRIPE_SECRET_KEY is not configured")
            messages.error(
                request,
                _(
                    "Payment processing is not currently available. "
                    "Please contact support."
                ),
            )
            return redirect("billing:plans")

        service = BillingService()

        success_url = request.build_absolute_uri(
            reverse("billing:checkout-success"),
        )
        cancel_url = request.build_absolute_uri(
            reverse("billing:plans"),
        )

        try:
            checkout_url = service.create_checkout_session(
                org=self.org,
                plan=plan,
                success_url=success_url,
                cancel_url=cancel_url,
                skip_trial=skip_trial,
            )
            return HttpResponseRedirect(checkout_url)
        except Exception as e:
            logger.exception("Failed to create checkout session")
            messages.error(
                request,
                _(
                    "Unable to start checkout. Please try again or contact support. "
                    f"Error: {e!s}"
                ),
            )
            return redirect("billing:plans")


class CheckoutSuccessView(LoginRequiredMixin, OrgMixin, TemplateView):
    """
    Handle successful Stripe Checkout return.

    This is the success_url Stripe redirects to after payment.
    The actual provisioning happens via webhook, but we show a success message.
    """

    template_name = "billing/checkout_success.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["org"] = self.org
        context["subscription"] = get_or_create_subscription(self.org)
        return context


class CustomerPortalView(LoginRequiredMixin, OrgMixin, RedirectView):
    """
    Redirect to Stripe Customer Portal for self-service management.

    Users can update payment methods, view invoices, cancel, etc.
    """

    permanent = False

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        from validibot.billing.services import BillingService

        subscription = get_or_create_subscription(self.org)

        # Check if we have a Stripe customer
        if not subscription.stripe_customer_id:
            messages.warning(
                request,
                _(
                    "No payment information on file. "
                    "Subscribe to a plan first to access billing management."
                ),
            )
            return redirect("billing:plans")

        # Check Stripe keys are configured
        if not settings.STRIPE_SECRET_KEY:
            logger.error("STRIPE_SECRET_KEY is not configured")
            messages.error(
                request,
                _(
                    "Payment management is not currently available. "
                    "Please contact support."
                ),
            )
            return redirect("billing:dashboard")

        service = BillingService()

        return_url = request.build_absolute_uri(
            reverse("billing:dashboard"),
        )

        try:
            portal_url = service.get_customer_portal_url(
                org=self.org,
                return_url=return_url,
            )
            return HttpResponseRedirect(portal_url)
        except Exception as e:
            logger.exception("Failed to create portal session")
            messages.error(
                request,
                _(
                    "Unable to access billing portal. Please try again or contact support. "
                    f"Error: {e!s}"
                ),
            )
            return redirect("billing:dashboard")
