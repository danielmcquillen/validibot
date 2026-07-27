from django.contrib import admin
from django.contrib.admin import register

from validibot.submissions.models import Submission


@register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "retention_safe_name",
        "org",
        "project",
        "user",
        "size_bytes",
        "created",
    )
    search_fields = ("user__email", "id")
    list_filter = ("created", "size_bytes")

    @admin.display(ordering="name", description="Name")
    def retention_safe_name(self, obj):
        """Show a submitter label only while its input policy permits access."""

        return obj.name if obj.is_content_viewable else ""

    def get_exclude(self, request, obj=None):
        """Hide submitted bytes and payload-derived context after the deadline."""
        exclude = list(super().get_exclude(request, obj) or [])
        if obj is not None and not obj.is_content_viewable:
            for field_name in (
                "name",
                "content",
                "input_file",
                "original_filename",
                "metadata",
            ):
                if field_name not in exclude:
                    exclude.append(field_name)
        return tuple(exclude) or None
