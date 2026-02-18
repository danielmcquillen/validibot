import copy

from django.http import Http404

from validibot.core.features import is_feature_enabled


class FeatureRequiredMixin:
    """
    Deny access (404) when a commercial feature is not enabled.

    Set ``required_feature`` on the view to a ``CommercialFeature`` value.
    This acts as defense-in-depth alongside URL-level gating.
    """

    required_feature: str = ""

    def dispatch(self, request, *args, **kwargs):
        if self.required_feature and not is_feature_enabled(self.required_feature):
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class BreadcrumbMixin:
    """
    A mixin to add breadcrumb navigation to a view.
    Override `get_breadcrumbs()` to return a list of breadcrumb items.
    Each breadcrumb is a dict with keys:
      - 'name': The text to display.
      - 'url': (Optional) The URL for that breadcrumb. For the current page,
          you may leave it empty.
    """

    # Define common breadcrumbs for all views.
    default_breadcrumbs = []
    # Views can override or extend this attribute.
    breadcrumbs = []

    def get_breadcrumbs(self) -> dict[str, str] | list[dict[str, str]]:
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


class FeaturedImageMixin:
    """
    Provides helper accessors for models that expose an optional `featured_image`
    FileField. Subclasses can override `featured_image_field_name` or
    `featured_image_alt_candidates` to tweak behaviour.
    """

    featured_image_field_name = "featured_image"
    featured_image_alt_candidates = (
        "featured_image_alt",
        "title",
        "name",
    )

    def get_featured_image_url(self) -> str | None:
        image_file = getattr(self, self.featured_image_field_name, None)
        if not image_file:
            return None
        try:
            return image_file.url
        except (ValueError, AttributeError):
            return None

    def get_featured_image_alt(self) -> str:
        for attr in self.featured_image_alt_candidates:
            value = getattr(self, attr, None)
            if not value:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    def has_featured_image(self) -> bool:
        return bool(self.get_featured_image_url())
