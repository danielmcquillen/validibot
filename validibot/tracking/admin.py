"""Read-only operational admin for product tracking events.

Tracking rows are derived telemetry, not configuration. Administrators need to
inspect them when diagnosing dashboards and event dispatch, but allowing rows
to be added, rewritten, or deleted through Django admin would make analytics
history unreliable. The registration therefore provides a searchable,
filterable view while disabling every mutation route.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib import admin
from django.utils.html import format_html

from validibot.tracking.constants import TrackingEventType
from validibot.tracking.models import TrackingEvent

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest
    from django.utils.safestring import SafeString


@admin.register(TrackingEvent)
class TrackingEventAdmin(admin.ModelAdmin):
    """Expose tracking history to operators without making it editable."""

    list_display = (
        "id",
        "created",
        "event_display",
        "event_type",
        "org",
        "project",
        "user",
    )
    list_filter = (
        "event_type",
        "app_event_type",
        ("org", admin.RelatedOnlyFieldListFilter),
        ("project", admin.RelatedOnlyFieldListFilter),
        ("created", admin.DateFieldListFilter),
    )
    search_fields = (
        "=id",
        "app_event_type",
        "org__name",
        "org__slug",
        "project__name",
        "user__email",
        "user__username",
    )
    list_select_related = ("org", "project", "user")
    ordering = ("-created",)
    date_hierarchy = "created"
    list_per_page = 50
    show_full_result_count = False
    readonly_fields = (
        "id",
        "event_type",
        "app_event_type",
        "org",
        "project",
        "user",
        "formatted_extra_data",
        "created",
        "modified",
    )
    fieldsets = (
        (
            "Event",
            {
                "fields": (
                    "id",
                    "event_type",
                    "app_event_type",
                    "created",
                    "modified",
                ),
            },
        ),
        (
            "Context",
            {
                "fields": (
                    "org",
                    "project",
                    "user",
                ),
            },
        ),
        (
            "Payload",
            {
                "fields": ("formatted_extra_data",),
            },
        ),
    )

    @admin.display(description="Event", ordering="app_event_type")
    def event_display(self, obj: TrackingEvent) -> str:
        """Return the specific application event or the generic event label."""
        if obj.event_type == TrackingEventType.APP_EVENT and obj.app_event_type:
            return obj.get_app_event_type_display()
        return obj.get_event_type_display()

    @admin.display(description="Extra data")
    def formatted_extra_data(self, obj: TrackingEvent) -> SafeString:
        """Render structured metadata as escaped, readable JSON."""
        if not obj.extra_data:
            return format_html("<span>(none)</span>")
        payload = json.dumps(
            obj.extra_data,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return format_html(
            '<pre style="margin:0; white-space:pre-wrap">{}</pre>',
            payload,
        )

    def get_queryset(self, request: HttpRequest) -> QuerySet[TrackingEvent]:
        """Load relationship columns in one query for the event list."""
        return super().get_queryset(request).select_related("org", "project", "user")

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Tracking rows must only be created by the tracking service."""
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: TrackingEvent | None = None,
    ) -> bool:
        """Prevent administrators from rewriting historical telemetry."""
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: TrackingEvent | None = None,
    ) -> bool:
        """Prevent deletion that would silently alter analytics history."""
        return False
