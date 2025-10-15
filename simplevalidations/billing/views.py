from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from simplevalidations.core.mixins import BreadcrumbMixin


class UsageAndBillingView(LoginRequiredMixin, BreadcrumbMixin, TemplateView):
    template_name = "billing/usage_and_billing.html"

    def get_breadcrumbs(self):
        return [
            {"name": _("Usage & Billing"), "url": ""},
        ]
