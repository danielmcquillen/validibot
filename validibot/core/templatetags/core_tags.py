import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from django import template
from django.conf import settings
from django.contrib.sites.shortcuts import get_current_site
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from validibot.core.utils import pretty_json
from validibot.core.utils import render_markdown_safe
from validibot.core.utils import reverse_with_org
from validibot.validations.constants import Severity
from validibot.workflows.constants import WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY

logger = logging.getLogger(__name__)


register = template.Library()


BRIGHTNESS_THRESHOLD = 128
MAX_HEX_COLOR_LENGTH = 6

# INCLUSION TAGS
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@register.inclusion_tag("core/partial/web_tracker.html", takes_context=True)
def web_tracker(context):
    """
    Include web tracker if conditions are met.

    We skip tracking in the following cases:
    - DEBUG mode is enabled
    - User is a superuser (unless TRACKER_INCLUDE_SUPERUSER is set)
    - Browser sends Global Privacy Control signal (Sec-GPC: 1)
    """
    if settings.DEBUG:
        include_tracker = False
    else:
        include_tracker = True
        try:
            request = getattr(context, "request", None)
            if request:
                # Honor Global Privacy Control signal
                if request.headers.get("Sec-GPC") == "1":
                    include_tracker = False
                else:
                    user = getattr(request, "user", None)
                    if user:
                        include_tracker = (
                            not user.is_superuser or settings.TRACKER_INCLUDE_SUPERUSER
                        )
        except Exception:
            include_tracker = not settings.DEBUG

    return {
        "include_tracker": include_tracker,
        "posthog_key": settings.POSTHOG_PROJECT_KEY,
        "posthog_api_host": settings.POSTHOG_API_HOST,
        "CSP_NONCE": context.get("CSP_NONCE", ""),
    }


# FILTERS
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@register.filter
def contrast_color(hex_color: str) -> str:
    """Given a hex color string, return either black or white depending on contrast."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != MAX_HEX_COLOR_LENGTH:
        return "#000000"  # Default to black if invalid

    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return "#000000" if brightness > BRIGHTNESS_THRESHOLD else "#FFFFFF"


@register.filter
def get_item(mapping, key):
    return mapping.get(key)


@register.filter(name="pretty_json")
def pretty_json_filter(value):
    """Pretty-print JSON/dicts for safe template display."""
    return pretty_json(value)


@register.filter(name="render_markdown")
def render_markdown_filter(value: str) -> str:
    """
    Render a Markdown string to sanitised HTML and mark it safe for templates.

    Sanitisation is performed by nh3 (via render_markdown_safe), so the
    returned value can be used directly in templates without the |safe filter.
    Supports bold, italic, links, lists, code, tables, and strikethrough.
    Script tags, event handlers, and other dangerous constructs are stripped.
    """
    return mark_safe(render_markdown_safe(value or ""))  # noqa: S308


# SIMPLE TAGS
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@register.simple_tag(takes_context=True)
def site_name(context) -> str | None:
    site_name = None
    request = context.get("request", None)
    if request:
        site = get_current_site(request)
        if site.name:
            return site.name
    else:
        logger.warning("site is not defined.")
    return site_name


@register.simple_tag(takes_context=True)
def active_link(context, nav_item_name):
    request = context.get("request", None)
    if request:
        return "active" if request.path.startswith(f"/app/{nav_item_name}/") else ""
    return ""


@register.simple_tag(takes_context=True)
def active_link_any(context, *nav_item_names):
    request = context.get("request", None)
    if not request:
        return ""
    for name in nav_item_names:
        if not name:
            continue
        prefix = f"/{name.strip('/')}/"
        if request.path.startswith(prefix):
            return "active"
    return ""


@register.simple_tag(takes_context=True)
def active_link_views(context, *view_names):
    """Return 'active' when the resolved view name matches any supplied values."""
    request = context.get("request", None)
    if not request:
        return ""

    match = getattr(request, "resolver_match", None)
    if not match:
        return ""

    current_view_name = (getattr(match, "view_name", "") or "").strip()
    current_url_name = (getattr(match, "url_name", "") or "").strip()
    if not (current_view_name or current_url_name):
        return ""

    normalized = {(name or "").strip() for name in view_names if (name or "").strip()}
    if current_view_name in normalized or current_url_name in normalized:
        return "active"
    return ""


@register.simple_tag(takes_context=True)
def user_settings_nav_state(context) -> dict[str, bool]:
    """Return navigation state booleans for the user settings menu."""

    request = context.get("request")
    state = {
        "active": False,
        "profile": False,
        "email": False,
        "api_key": False,
        "security": False,
        "organizations": False,
    }
    if not request:
        return state

    match = getattr(request, "resolver_match", None)
    if not match:
        return state

    url_name = (getattr(match, "url_name", "") or "").strip()
    view_name = (getattr(match, "view_name", "") or "").strip()

    def is_match(name: str) -> bool:
        return url_name == name or view_name == f"users:{name}"

    state["profile"] = is_match("profile")
    state["email"] = is_match("email")
    state["api_key"] = is_match("api-key")
    # Keep "Security" highlighted during multi-step allauth MFA flows
    # (mfa_index, mfa_activate_totp, mfa_generate_recovery_codes, ...).
    state["security"] = (
        is_match("security")
        or url_name.startswith("mfa_")
        or view_name.startswith("mfa_")
    )
    state["organizations"] = view_name.startswith("users:organization-")
    state["active"] = any(
        state[key]
        for key in ("profile", "email", "api_key", "security", "organizations")
    )
    return state


@register.simple_tag(takes_context=True)
def active_builder_link(context, nav_item_name):
    request = context.get("request", None)
    if request:
        request = context["request"]
        return "active" if request.path.startswith(f"/builder/{nav_item_name}/") else ""
    return ""


@register.simple_tag(takes_context=True)
def org_url(context, view_name, *args, **kwargs):
    request = context.get("request")
    resolved_kwargs = dict(kwargs or {})
    return reverse_with_org(
        view_name,
        request=request,
        args=args,
        kwargs=resolved_kwargs,
    )


@register.simple_tag(takes_context=True)
def mfa_breadcrumbs(context, leaf_label: str) -> list[dict[str, str]]:
    """Build a breadcrumb trail for an allauth MFA management page.

    Allauth views don't run through our ``BreadcrumbMixin``, so pages
    that extend ``app_base.html`` directly need to supply the trail
    themselves. This tag returns the standard ``User Settings ›
    Security › {leaf_label}`` shape, with the ``users:security`` link
    scoped to the current org via ``reverse_with_org``.

    Example::

        {% mfa_breadcrumbs "Set up authenticator" as breadcrumbs %}
        {% include "app/partial/components/app_top_bar.html" %}
    """
    request = context.get("request")
    return [
        {
            "name": _("User Settings"),
            "url": reverse_with_org("users:profile", request=request),
        },
        {
            "name": _("Security"),
            "url": reverse_with_org("users:security", request=request),
        },
        {"name": leaf_label, "url": ""},
    ]


@register.simple_tag(takes_context=True)
def workflow_launch_preferred_mode(context) -> str:
    default_mode = "upload"
    request = context.get("request")
    if not request:
        return default_mode
    try:
        preferred = request.session.get(WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to read workflow launch preference from session.")
        return default_mode
    if preferred in {"upload", "paste", "form"}:
        return preferred
    return default_mode


@register.simple_tag
def finding_badge_class(finding) -> str:
    """
    Return the bootstrap badge class appropriate for a finding's severity.
    """

    severity = getattr(finding, "severity", "") or ""
    if isinstance(severity, Severity):
        severity_value = severity.value
    else:
        severity_value = str(severity).upper()
    mapping = {
        Severity.SUCCESS: "text-bg-success",
        Severity.ERROR: "text-bg-danger",
        Severity.WARNING: "text-bg-warning text-dark",
        Severity.INFO: "text-bg-secondary",
        "SUCCESS": "text-bg-success",
        "ERROR": "text-bg-danger",
        "WARNING": "text-bg-warning text-dark",
        "INFO": "text-bg-secondary",
    }
    return mapping.get(severity_value, "text-bg-secondary")


@register.simple_tag
def app_version() -> str:
    """Return the validibot package version."""
    from validibot import __version__

    return __version__


@register.simple_tag
def shared_version() -> str:
    """Return the installed validibot-shared package version."""
    try:
        return pkg_version("validibot-shared")
    except PackageNotFoundError:
        return "?"


@register.inclusion_tag("core/partial/help_button.html")
def help_button(slug: str, sr_label: str = "") -> dict:
    """Render a small info-circle button that opens the help drawer.

    Usage::

        {% help_button "output-hash" %}
        {% help_button "output-hash" sr_label="What is the output hash?" %}

    The button uses HTMX to fetch the help drawer content partial into the
    shared offcanvas container, then shows it via Bootstrap's JS API.
    """
    from django.urls import reverse

    return {
        "slug": slug,
        "sr_label": sr_label or f"Help: {slug}",
        "help_url": reverse("core:help_drawer", kwargs={"slug": slug}),
    }


def _cloud_tos_url() -> str:
    """Resolve the cloud TOS URL, or return empty string if cloud not installed."""
    try:
        from django.urls import reverse

        return reverse("cloud-accounts:terms")
    except Exception:
        return ""


@register.inclusion_tag("core/partial/cloud_tos_nav_link.html")
def cloud_tos_nav_link():
    """
    Render a "Terms of Service" link for the left sidebar nav.

    Outputs nothing if the cloud layer is not installed. Used in
    app_left_nav.html so templates stay clean.
    """
    return {"tos_url": _cloud_tos_url()}


@register.inclusion_tag("core/partial/cloud_tos_dropdown_link.html")
def cloud_tos_dropdown_link():
    """
    Render a "Terms of Service" link for the user profile dropdown.

    Outputs nothing if the cloud layer is not installed. Used in
    app_top_bar.html so templates stay clean.
    """
    return {"tos_url": _cloud_tos_url()}
