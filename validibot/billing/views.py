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
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import RedirectView
from django.views.generic import TemplateView

from validibot.billing.constants import SubscriptionStatus
from validibot.billing.metering import BasicWorkflowMeter
from validibot.billing.models import Plan
from validibot.users.mixins import OrgMixin

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http import HttpResponse

logger = logging.getLogger(__name__)


class BillingDashboardView(LoginRequiredMixin, OrgMixin, TemplateView):
    """
    Main billing dashboard showing subscription status and usage.

    Shows:
    - Current plan and subscription status
    - Trial countdown (if applicable)
    - Usage summary (basic launches, credits)
    - Upgrade options
    """

    template_name = "billing/dashboard.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        subscription = self.org.subscription
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

        context.update({
            "subscription": subscription,
            "plan": plan,
            "is_trial": is_trial,
            "trial_days_remaining": trial_days_remaining,
            "basic_usage": usage,
            "credits_balance": subscription.total_credits_balance,
            "all_plans": Plan.objects.all(),
            "can_upgrade": plan.code != "ENTERPRISE",
            "stripe_public_key": settings.STRIPE_PUBLIC_KEY,
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
        subscription = self.org.subscription

        # Get usage from trial period
        meter = BasicWorkflowMeter()
        usage = meter.get_usage(self.org)

        context.update({
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
    """

    permanent = False

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        from validibot.billing.services import BillingService

        plan_code = request.GET.get("plan", "STARTER")

        try:
            plan = Plan.objects.get(code=plan_code)
        except Plan.DoesNotExist:
            logger.warning("Invalid plan code: %s", plan_code)
            return redirect("billing:dashboard")

        if not plan.stripe_price_id:
            logger.error("Plan %s has no stripe_price_id", plan.code)
            return redirect("billing:dashboard")

        service = BillingService()

        success_url = request.build_absolute_uri(
            reverse("billing:checkout-success"),
        )
        cancel_url = request.build_absolute_uri(
            reverse("billing:dashboard"),
        )

        try:
            checkout_url = service.create_checkout_session(
                org=self.org,
                plan=plan,
                success_url=success_url,
                cancel_url=cancel_url,
            )
            return HttpResponseRedirect(checkout_url)
        except Exception:
            logger.exception("Failed to create checkout session")
            return redirect("billing:dashboard")


class CheckoutSuccessView(LoginRequiredMixin, OrgMixin, TemplateView):
    """
    Handle successful Stripe Checkout return.

    This is the success_url Stripe redirects to after payment.
    The actual provisioning happens via webhook, but we show a success message.
    """

    template_name = "billing/checkout_success.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["subscription"] = self.org.subscription
        return context


class CustomerPortalView(LoginRequiredMixin, OrgMixin, RedirectView):
    """
    Redirect to Stripe Customer Portal for self-service management.

    Users can update payment methods, view invoices, cancel, etc.
    """

    permanent = False

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        from validibot.billing.services import BillingService

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
        except Exception:
            logger.exception("Failed to create portal session")
            return redirect("billing:dashboard")
