from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.views import View

from roscoe.core.mixins import BreadcrumbMixin


class MyDashboardView(LoginRequiredMixin, BreadcrumbMixin, View):
    breadcrumbs = [
        {
            "name": "My Dashboard",
            "url": "",
        },
    ]

    def get(self, request):
        breadcrumbs = self.get_breadcrumbs()
        context = {
            "breadcrumbs": breadcrumbs,
        }
        return render(request, "dashboard/my_dashboard.html", context)
