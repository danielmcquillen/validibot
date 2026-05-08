import copy

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils.translation import gettext_lazy as _

from validibot.core.features import is_feature_enabled


class FeatureRequiredMixin:
    """
    Deny access (404) when a commercial feature is not enabled.

    Set ``required_commercial_feature`` on the view to a
    ``CommercialFeature`` value.
    This acts as defense-in-depth alongside URL-level gating.
    """

    required_commercial_feature: str = ""

    def dispatch(self, request, *args, **kwargs):
        if self.required_commercial_feature and not is_feature_enabled(
            self.required_commercial_feature,
        ):
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class GuestInvitesEnabledMixin:
    """Block access (403) when ``SiteSettings.allow_guest_invites`` is False.

    Mix into any view that either *creates* or *accepts* a guest invite.
    The site-wide kill-switch is a two-sided gate — it stops new invites
    AND blocks redemption of pending invites — so an operator flipping
    the flag during incident response cannot have an in-flight invite
    sneak through. Two-sided enforcement makes the toggle atomic from
    the operator's perspective.

    Superusers are intentionally not bypassed. The flag is platform-
    wide and operator-controlled; if the operator wants to explicitly
    re-enable invites for themselves, they flip the flag back on.

    The check runs first in ``dispatch`` so it composes with downstream
    mixins (``FeatureRequiredMixin``, ``OrganizationPermissionRequiredMixin``,
    etc.) without ordering surprises — denied invites never see a
    permission check or feature lookup.
    """

    def dispatch(self, request, *args, **kwargs):
        # Local import keeps this mixin importable in environments
        # where the core app's models aren't loaded yet (e.g. during
        # migrations bootstrap).
        from validibot.core.site_settings import get_site_settings

        if not get_site_settings().allow_guest_invites:
            raise PermissionDenied(
                _(
                    "Guest invites are currently disabled site-wide. "
                    "Contact your administrator if you need to send or "
                    "accept a guest invite.",
                ),
            )
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
