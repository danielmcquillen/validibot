"""
URL configuration for the billing app.

Routes:
- /app/billing/                 - Billing dashboard
- /app/billing/plans/           - Plan comparison / upgrade page
- /app/billing/trial-expired/   - Trial expired conversion page
- /app/billing/checkout/        - Start Stripe Checkout
- /app/billing/checkout/success/ - Checkout success return
- /app/billing/portal/          - Stripe Customer Portal redirect
"""

from django.urls import path

from validibot.billing.views import BillingDashboardView
from validibot.billing.views import CheckoutStartView
from validibot.billing.views import CheckoutSuccessView
from validibot.billing.views import CustomerPortalView
from validibot.billing.views import PlansView
from validibot.billing.views import TrialExpiredView

app_name = "billing"

urlpatterns = [
    path(
        "",
        BillingDashboardView.as_view(),
        name="dashboard",
    ),
    path(
        "plans/",
        PlansView.as_view(),
        name="plans",
    ),
    path(
        "trial-expired/",
        TrialExpiredView.as_view(),
        name="trial-expired",
    ),
    path(
        "checkout/",
        CheckoutStartView.as_view(),
        name="checkout",
    ),
    path(
        "checkout/success/",
        CheckoutSuccessView.as_view(),
        name="checkout-success",
    ),
    path(
        "portal/",
        CustomerPortalView.as_view(),
        name="portal",
    ),
]
