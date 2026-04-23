"""Views for the advanced analytics dashboard (bare-bones).

Today this is a single placeholder page — the scaffold a future
reporting dashboard will plug into. It exists now so the nav entry
has somewhere to link and the feature flag has a URL surface.

Same three gates as every other Pro customer surface:

1. :class:`FeatureRequiredMixin` — 404 when the deployment's license
   doesn't advertise ``ADVANCED_ANALYTICS``.
2. :class:`LoginRequiredMixin` — anonymous traffic redirects to login.
3. :class:`OrgMixin` — scope to ``request.active_org``.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from validibot.core.features import CommercialFeature
from validibot.core.mixins import BreadcrumbMixin
from validibot.core.mixins import FeatureRequiredMixin
from validibot.users.mixins import OrgMixin


class AdvancedAnalyticsDashboardView(
    LoginRequiredMixin,
    FeatureRequiredMixin,
    OrgMixin,
    BreadcrumbMixin,
    TemplateView,
):
    """Placeholder landing page for the advanced analytics dashboard.

    Renders a "coming soon" card with the planned reports listed. No
    querysets yet — when we ship real reports, each becomes its own
    view + template partial composed into this dashboard.
    """

    template_name = "analytics/advanced_analytics_dashboard.html"
    required_commercial_feature = CommercialFeature.ADVANCED_ANALYTICS
    breadcrumbs = [{"name": "Advanced Analytics"}]
