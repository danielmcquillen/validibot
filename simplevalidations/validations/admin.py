from django.contrib import admin

from simplevalidations.validations.models import Ruleset, ValidationRun, Validator


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


@admin.register(Validator)
class ValidatorAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "slug",
        "order",
        "validation_type",
        "version",
        "created",
        "modified",
    )
    list_filter = ("validation_type", "created", "modified")
    search_fields = ("name", "slug", "version")
    ordering = ("order",)


@admin.register(ValidationRun)
class ValidationRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "submission",
        "workflow",
        "org",
        "status",
        "created",
        "modified",
    )
    list_filter = ("org", "status", "created", "modified")
    search_fields = ("submission__id", "workflow__name", "org__name")
    ordering = ("-created",)
