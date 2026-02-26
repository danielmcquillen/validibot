from django.contrib import admin

from validibot.validations.models import CustomValidator
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
from validibot.validations.models import ValidatorResourceFile


@admin.register(Ruleset)
class RulesetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "org",
        "ruleset_type",
        "version",
        "created",
        "modified",
    )
    list_filter = ("org", "ruleset_type", "created", "modified")
    search_fields = ("name", "org__name", "version")
    ordering = ("-created",)


@admin.register(RulesetAssertion)
class RulesetAssertionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "ruleset",
        "assertion_type",
        "operator",
        "target_catalog_entry",
        "target_data_path",
        "severity",
        "order",
        "created",
    )
    list_filter = ("assertion_type", "operator", "severity")
    search_fields = (
        "ruleset__name",
        "target_catalog_entry__slug",
        "target_data_path",
    )


@admin.register(Validator)
class ValidatorAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "slug",
        "org",
        "order",
        "validation_type",
        "version",
        "is_system",
        "is_enabled",
        "allow_custom_assertion_targets",
        "created",
        "modified",
    )
    list_filter = ("validation_type", "is_system", "is_enabled", "created", "modified")
    list_editable = ("is_enabled",)
    search_fields = ("name", "slug", "version", "org__name")
    ordering = ("order",)


@admin.register(ValidatorCatalogEntry)
class ValidatorCatalogEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "validator",
        "entry_type",
        "run_stage",
        "slug",
        "data_type",
        "is_required",
        "order",
        "created",
    )
    list_filter = ("entry_type", "run_stage", "data_type", "is_required")
    search_fields = ("validator__name", "slug", "label")
    ordering = ("validator", "entry_type", "run_stage", "order")


@admin.register(CustomValidator)
class CustomValidatorAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "validator",
        "org",
        "custom_type",
        "base_validation_type",
        "created",
        "modified",
    )
    list_filter = ("custom_type", "base_validation_type")
    search_fields = ("validator__name", "org__name")


@admin.register(ValidatorResourceFile)
class ValidatorResourceFileAdmin(admin.ModelAdmin):
    """
    Admin for managing validator resource files (weather files, libraries, etc.).

    Resource files can be:
    - System-wide (org=NULL): visible to all organizations
    - Org-specific (org=<uuid>): visible only to that organization
    """

    list_display = (
        "name",
        "validator",
        "resource_type",
        "scope_display",
        "is_default",
        "filename",
        "created",
    )
    list_filter = (
        "resource_type",
        "is_default",
        "validator",
        ("org", admin.RelatedOnlyFieldListFilter),
    )
    search_fields = ("name", "filename", "validator__name", "org__name")
    ordering = ("validator", "-is_default", "name")
    readonly_fields = ("id", "created", "modified")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "validator",
                    "resource_type",
                    "file",
                ),
            },
        ),
        (
            "Scope",
            {
                "fields": ("org", "is_default"),
                "description": (
                    "Leave 'Org' empty for system-wide resources visible to all "
                    "organizations. Set an org to restrict visibility to that org only."
                ),
            },
        ),
        (
            "Details",
            {
                "fields": ("filename", "description", "metadata"),
                "classes": ("collapse",),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("id", "created", "modified"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Scope")
    def scope_display(self, obj):
        """Display whether resource is system-wide or org-specific."""
        if obj.org is None:
            return "System-wide"
        return f"Org: {obj.org.name}"


class ValidationStepRunInline(admin.TabularInline):
    """Inline view of step runs for a validation run."""

    model = ValidationStepRun
    extra = 0
    readonly_fields = (
        "workflow_step",
        "status",
        "started_at",
        "ended_at",
        "duration_ms",
        "error",
    )
    fields = (
        "step_order",
        "workflow_step",
        "status",
        "started_at",
        "ended_at",
        "duration_ms",
        "error",
    )
    ordering = ("step_order",)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ValidationRun)
class ValidationRunAdmin(admin.ModelAdmin):
    """
    Admin view for ValidationRun with operator-focused features.

    Provides quick access to run status, error details, and step information
    for debugging failed validations.
    """

    list_display = (
        "short_id",
        "workflow_name",
        "org",
        "status",
        "error_category",
        "duration_display",
        "created",
    )
    list_filter = (
        "status",
        "error_category",
        "source",
        "org",
        ("created", admin.DateFieldListFilter),
    )
    search_fields = (
        "id",
        "submission__id",
        "workflow__name",
        "org__name",
        "error",
    )
    ordering = ("-created",)
    readonly_fields = (
        "id",
        "org",
        "workflow",
        "project",
        "user",
        "submission",
        "status",
        "error_category",
        "error",
        "user_friendly_error",
        "started_at",
        "ended_at",
        "duration_ms",
        "source",
        "created",
        "modified",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "status",
                    "error_category",
                    "user_friendly_error",
                ),
            },
        ),
        (
            "Details",
            {
                "fields": (
                    "org",
                    "workflow",
                    "project",
                    "user",
                    "submission",
                    "source",
                ),
            },
        ),
        (
            "Timing",
            {
                "fields": (
                    "started_at",
                    "ended_at",
                    "duration_ms",
                ),
            },
        ),
        (
            "Error Details",
            {
                "fields": ("error",),
                "classes": ("collapse",),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created", "modified"),
                "classes": ("collapse",),
            },
        ),
    )
    inlines = [ValidationStepRunInline]
    date_hierarchy = "created"
    list_per_page = 50

    @admin.display(description="ID")
    def short_id(self, obj):
        """Display shortened UUID for readability."""
        return str(obj.id)[:8]

    @admin.display(description="Workflow")
    def workflow_name(self, obj):
        """Display workflow name."""
        return obj.workflow.name if obj.workflow else "-"

    @admin.display(description="Duration")
    def duration_display(self, obj):
        """Display duration in human-readable format."""
        ms_per_second = 1000
        ms_per_minute = 60000
        if not obj.duration_ms:
            return "-"
        if obj.duration_ms < ms_per_second:
            return f"{obj.duration_ms}ms"
        if obj.duration_ms < ms_per_minute:
            return f"{obj.duration_ms / ms_per_second:.1f}s"
        return f"{obj.duration_ms / ms_per_minute:.1f}m"

    def has_add_permission(self, request):
        """Runs are created by the system, not manually."""
        return False

    def has_change_permission(self, request, obj=None):
        """Runs are read-only for operators."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Allow superusers to delete runs for cleanup."""
        return request.user.is_superuser
