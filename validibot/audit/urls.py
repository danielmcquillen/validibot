"""URL patterns for the Pro-gated audit log views.

Mounted in ``config/urls_web.py`` under the ``audit`` namespace, so
reverse names read as ``audit:list`` / ``audit:detail`` /
``audit:export``.

The export path is declared before the integer-id detail path so the
URL resolver never mistakes ``export`` for an ``entry_id``. Django's
resolver picks the first match in declaration order and the converter
for ``<int:entry_id>`` would actually fail for a non-integer, but
keeping the explicit string route earlier is clearer to readers.
"""

from django.urls import path

from validibot.audit import views

app_name = "audit"

urlpatterns = [
    path(
        "",
        views.AuditLogListView.as_view(),
        name="list",
    ),
    path(
        "export/",
        views.AuditLogExportView.as_view(),
        name="export",
    ),
    path(
        "<int:entry_id>/",
        views.AuditLogDetailView.as_view(),
        name="detail",
    ),
]
