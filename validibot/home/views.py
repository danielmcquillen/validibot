"""Views for the home app."""

from django.shortcuts import redirect
from django.views.generic import TemplateView


class HomeView(TemplateView):
    """Landing page view that redirects authenticated users to the dashboard.

    For self-hosted deployments, the home page provides a simple welcome message
    and login link. Authenticated users are redirected to their dashboard.
    """

    template_name = "home/home.html"

    def get(self, request, *args, **kwargs):
        """Redirect authenticated users to the app dashboard."""
        if request.user.is_authenticated:
            return redirect("dashboard:my_dashboard")
        return super().get(request, *args, **kwargs)
