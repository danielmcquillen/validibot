from django.contrib import admin

from simplevalidations.validations.models import CustomValidator
from simplevalidations.validations.models import Ruleset
from simplevalidations.validations.models import RulesetAssertion
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.models import Validator
from simplevalidations.validations.models import ValidatorCatalogEntry


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
        "target_catalog",
        "target_field",
        "severity",
        "order",
        "created",
    )
    list_filter = ("assertion_type", "operator", "severity")
    search_fields = (
        "ruleset__name",
        "target_catalog__slug",
        "target_field",
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
        "allow_custom_assertion_targets",
        "created",
        "modified",
    )
    list_filter = ("validation_type", "is_system", "created", "modified")
    search_fields = ("name", "slug", "version", "org__name")
    ordering = ("order",)


@admin.register(ValidatorCatalogEntry)
class ValidatorCatalogEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "validator",
        "entry_type",
        "slug",
        "data_type",
        "is_required",
        "order",
        "created",
    )
    list_filter = ("entry_type", "data_type", "is_required")
    search_fields = ("validator__name", "slug", "label")
    ordering = ("validator", "entry_type", "order")


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
