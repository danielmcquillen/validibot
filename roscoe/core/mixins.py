import copy
from typing import Dict

from django.urls import reverse_lazy


class BreadcrumbMixin:
    """
    A mixin to add breadcrumb navigation to a view.
    Override `get_breadcrumbs()` to return a list of breadcrumb items.
    Each breadcrumb is a dict with keys:
      - 'name': The text to display.
      - 'url': (Optional) The URL for that breadcrumb. For the current page, you may leave it empty.
    """

    # Define common breadcrumbs for all views.
    default_breadcrumbs = [
        {"name": "Home", "url": reverse_lazy("marketing:home")},
    ]
    # Views can override or extend this attribute.
    breadcrumbs = []

    def get_breadcrumbs(self) -> Dict:
        """
        Returns a list of breadcrumb dictionaries.
        By default, it returns the `breadcrumbs` attribute.
        Override this method in your view for dynamic breadcrumbs.
        """
        breadcrumbs = self.default_breadcrumbs + self.breadcrumbs
        return copy.deepcopy(breadcrumbs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["breadcrumbs"] = self.get_breadcrumbs()
        return context
