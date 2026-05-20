from django.contrib import admin
from django.contrib.admin import register

from validibot.submissions.models import Submission


@register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "org",
        "project",
        "user",
        "size_bytes",
        "created",
    )
    search_fields = ("name", "user__email", "id")
    list_filter = ("created", "size_bytes")

    def get_exclude(self, request, obj=None):
        """Hide raw submitted data when retention no longer allows viewing."""
        exclude = list(super().get_exclude(request, obj) or [])
        if obj is not None and not obj.is_content_viewable:
            for field_name in ("content", "input_file"):
                if field_name not in exclude:
                    exclude.append(field_name)
        return tuple(exclude) or None
