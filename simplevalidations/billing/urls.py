from django.urls import path

from simplevalidations.billing.views import UsageAndBillingView

app_name = "billing"

urlpatterns = [
    path("usage/", UsageAndBillingView.as_view(), name="usage-and-billing"),
]
