# help/views.py

from django.conf import settings
from django.contrib.flatpages.models import FlatPage
from django.contrib.sites.models import Site
from django.contrib.sites.shortcuts import get_current_site
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _

# Expected in your settings.py, e.g.:
#
# HELP_SECTIONS = [
#     ("getting-started", "Getting started"),
#     ("workflows", "Workflows"),
#     ("validators", "Validators"),
#     ("admin", "Admin"),
# ]
#
# If it's missing, we provide a fallback so things still work.
DEFAULT_HELP_SECTIONS = [
    ("getting-started", _("Getting started")),
    ("workflows", _("Workflows")),
    ("validators", _("Validators")),
    ("concepts", _("Concepts")),
    ("admin", _("Workspace")),
]

SECTION_LIST = getattr(settings, "HELP_SECTIONS", DEFAULT_HELP_SECTIONS)
SECTION_INDEX = {slug: idx for idx, (slug, _label) in enumerate(SECTION_LIST)}
SECTION_LABEL = {slug: label for slug, label in SECTION_LIST}
HELP_URL_PREFIX = "/app/help/"


def _section_slug_from_url(url: str) -> str:
    """
    Extract a section slug from a FlatPage.url.

    Examples:
      "/app/help/index/"                  -> "index"
      "/app/help/getting-started/"        -> "getting-started"
      "/app/help/workflows/creating/"     -> "workflows"
      "/app/help/admin/organizations/"    -> "admin"
    """
    # Strip the "/app/help/" prefix and trailing slash
    if not url.startswith(HELP_URL_PREFIX):
        # Fallback: treat everything else as index
        return "getting-started"

    path = url[len(HELP_URL_PREFIX) :].strip("/")  # "index", "workflows/creating", etc.

    if not path or path == "index":
        return "index"

    first_segment = path.split("/")[0]
    return first_segment


def _section_label_from_slug(slug: str) -> str:
    """
    Map a section slug to a human label using HELP_SECTIONS,
    falling back to Title Case if not configured.
    """
    if slug == "index":
        return _("Index")
    if slug in SECTION_LABEL:
        return SECTION_LABEL[slug]
    return slug.replace("-", " ").title()


def help_page(request, path: str = "index"):
    """
    Main help view.

    URL patterns (see help/urls.py) should route:
      /app/help/                 -> help_page(path="index")
      /app/help/getting-started/ -> help_page(path="getting-started/")
      /app/help/workflows/run/   -> help_page(path="workflows/run/")

    This view:
      - Resolves the FlatPage for /app/help/<path>/
      - Builds an ordered list of all help pages for the nav
      - Passes `page` and `nav_items` into the template
    """
    if not path:
        path = "index/"
    elif not path.endswith("/"):
        path += "/"

    full_url = f"{HELP_URL_PREFIX}{path}"

    site = get_current_site(request)
    if not isinstance(site, Site):
        site = Site.objects.get_current()

    # Current page
    page = get_object_or_404(
        FlatPage.objects.filter(sites=site),
        url=full_url,
    )

    # All help pages for navigation
    nav_pages = FlatPage.objects.filter(
        sites=site,
        url__startswith=HELP_URL_PREFIX,
    ).order_by("url")

    index_item = None
    nav_items = []
    for p in nav_pages:
        section_slug = _section_slug_from_url(p.url)
        section_label = _section_label_from_slug(section_slug)
        section_index = SECTION_INDEX.get(section_slug, 999)  # unknowns go last

        item = {
            "page": p,
            "section_slug": section_slug,
            "section_label": section_label,
            "section_index": section_index,
        }

        if section_slug == "index":
            index_item = item
            continue

        nav_items.append(item)

    # Sort by section order, then by page title within the section
    nav_items.sort(
        key=lambda item: (
            item["section_index"],
            item["page"].title.lower(),
        )
    )

    context = {
        "page": page,
        "nav_items": nav_items,
        "index_item": index_item,
    }

    return render(request, "help/page.html", context)
