"""Report layout preference — which validation-report layout the user sees.

The run report renders in one of two layouts, both saved as template partials:

* ``stacked``  — full-width, three-section accordion (Run Summary, User Inputs,
  Outputs). The default.
* ``classic``  — the original two-column (findings left, summary sidebar right).

Both the standalone run-detail page and the launch-page status card render the
same report, so the choice lives in **one** place — the session — and a single
helper resolves it for every view. A ``?layout=`` query param (emitted by the
toggle in ``_report_layout_toggle.html``) updates the stored preference; absent
that, the last stored choice wins, falling back to ``stacked``.

Session, not a user-profile field, on purpose: it's a transient view
preference, not durable account state — cheap to set, scoped to the browser
session, and it needs no migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.http import HttpRequest

REPORT_LAYOUTS: tuple[str, ...] = ("stacked", "classic")
DEFAULT_REPORT_LAYOUT = "stacked"
REPORT_LAYOUT_SESSION_KEY = "report_layout"


def resolve_report_layout(request: HttpRequest) -> str:
    """Return the effective report layout, honouring and persisting ``?layout``.

    If the request carries a valid ``?layout=stacked|classic``, that choice is
    written to the session (so it sticks for subsequent views without the
    param). The returned value is always one of :data:`REPORT_LAYOUTS`, defaulting
    to :data:`DEFAULT_REPORT_LAYOUT` when nothing valid is stored — so a caller
    can use it directly in ``{% if report_layout == "classic" %}`` without
    re-validating.
    """
    requested = request.GET.get("layout")
    if requested in REPORT_LAYOUTS:
        request.session[REPORT_LAYOUT_SESSION_KEY] = requested

    stored = request.session.get(REPORT_LAYOUT_SESSION_KEY, DEFAULT_REPORT_LAYOUT)
    return stored if stored in REPORT_LAYOUTS else DEFAULT_REPORT_LAYOUT
