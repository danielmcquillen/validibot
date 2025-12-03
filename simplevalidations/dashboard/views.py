from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import redirect
from django.shortcuts import render
from django.template.response import TemplateResponse
from django.utils.translation import gettext_lazy as _
from django.views import View

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.dashboard.time_ranges import iter_time_range_options
from simplevalidations.dashboard.time_ranges import resolve_time_range
from simplevalidations.dashboard.widgets import registry
from simplevalidations.dashboard.widgets.base import WidgetRegistrationError
from simplevalidations.users.constants import PermissionCode


def _has_dashboard_access(request) -> bool:
    membership = getattr(request, "active_membership", None)
    if not membership and hasattr(request.user, "membership_for_current_org"):
        membership = request.user.membership_for_current_org()
    organization = getattr(membership, "org", None)
    if not membership or not organization:
        return False
    return request.user.has_perm(
        PermissionCode.ANALYTICS_VIEW.value,
        organization,
    )


class MyDashboardView(LoginRequiredMixin, BreadcrumbMixin, View):
    breadcrumbs = [
        {
            "name": "My Dashboard",
            "url": "",
        },
    ]

    def dispatch(self, request, *args, **kwargs):
        if not _has_dashboard_access(request):
            messages.error(
                request,
                _(
                    "Dashboard insights are available to organization owners, "
                    "admins, and authors."
                ),
            )
            return redirect(
                reverse_with_org("workflows:workflow_list", request=request)
            )
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        breadcrumbs = self.get_breadcrumbs()
        time_range_slug = request.GET.get("time_range")
        time_range = resolve_time_range(time_range_slug)
        widget_definitions = list(registry)
        context = {
            "breadcrumbs": breadcrumbs,
            "time_range": time_range,
            "time_range_options": tuple(iter_time_range_options()),
            "widget_definitions": widget_definitions,
        }
        return render(request, "dashboard/my_dashboard.html", context)


class WidgetDetailView(LoginRequiredMixin, BreadcrumbMixin, View):
    """
    HTMX endpoint that renders a single widget body.
    """

    def dispatch(self, request, *args, **kwargs):
        if not _has_dashboard_access(request):
            return redirect(
                reverse_with_org("workflows:workflow_list", request=request)
            )
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug: str):
        time_range_slug = request.GET.get("time_range")
        time_range = resolve_time_range(time_range_slug)
        try:
            definition = registry.get(slug)
        except WidgetRegistrationError as exc:
            raise Http404(str(exc)) from exc

        widget = definition.instantiate(request=request, time_range=time_range)
        context = widget.as_context()
        context.update(
            {
                "time_range": time_range,
                "definition": definition,
            },
        )
        return TemplateResponse(request, definition.template_name, context)
