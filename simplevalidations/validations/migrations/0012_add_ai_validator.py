from __future__ import annotations

from django.db import migrations


AI_VALIDATION_TYPE = "AI_ASSIST"


def create_ai_validator(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")

    validator, created = Validator.objects.get_or_create(
        validation_type=AI_VALIDATION_TYPE,
        slug="ai-assist",
        defaults={
            "name": "AI Assist",
            "description": "AI-assisted policy and critique validator.",
        },
    )
    if created:
        # Leave default_ruleset unset intentionally
        validator.save(update_fields=["name", "description"])


def delete_ai_validator(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    Validator.objects.filter(validation_type=AI_VALIDATION_TYPE, slug="ai-assist").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0011_remove_validationfinding_validations_step_ru_a534f9_idx_and_more"),
    ]

    operations = [
        migrations.RunPython(create_ai_validator, delete_ai_validator),
    ]
