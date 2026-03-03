"""Views for the home app."""

from django.shortcuts import redirect
from django.views.generic import TemplateView


class HomeView(TemplateView):
    """Landing page view that redirects all users appropriately.

    Authenticated users go to the dashboard. Unauthenticated users go directly
    to the sign-in page, which now carries the landing page hero design.
    """

    template_name = "home/home.html"

    def get(self, request, *args, **kwargs):
        """Redirect users: authenticated → dashboard, unauthenticated → sign-in."""
        if request.user.is_authenticated:
            return redirect("dashboard:my_dashboard")
        return redirect("account_login")
