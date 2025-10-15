from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import render
from django.template.response import TemplateResponse
from django.views import View

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.dashboard.time_ranges import iter_time_range_options
from simplevalidations.dashboard.time_ranges import resolve_time_range
from simplevalidations.dashboard.widgets import registry
from simplevalidations.dashboard.widgets.base import WidgetRegistrationError


class MyDashboardView(LoginRequiredMixin, BreadcrumbMixin, View):
    breadcrumbs = [
        {
            "name": "My Dashboard",
            "url": "",
        },
    ]

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
