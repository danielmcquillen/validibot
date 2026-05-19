"""
Enable step-level assertions for JSON Schema and XML Schema validators.

These validators still run their schema validation first, but workflow authors
can now add per-step BASIC/CEL assertions afterward for business rules that do
not belong in the schema itself. Existing database rows need the capability flag
flipped so the step editor and assertion endpoints become available without
requiring operators to run a separate catalog sync.
"""

from django.db import migrations
from django.db import models


_SCHEMA_VALIDATOR_TYPES = ["JSON_SCHEMA", "XML_SCHEMA"]


def enable_schema_assertions(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    Validator.objects.filter(
        validation_type__in=_SCHEMA_VALIDATOR_TYPES,
    ).update(supports_assertions=True)


def disable_schema_assertions(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    Validator.objects.filter(
        validation_type__in=_SCHEMA_VALIDATOR_TYPES,
    ).update(supports_assertions=False)


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0049_add_shacl_assertion_choices"),
    ]

    operations = [
        migrations.AlterField(
            model_name="validator",
            name="supports_assertions",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when this validator supports step-level assertions "
                    "(Basic, CEL, or validator-specific assertion types)."
                ),
            ),
        ),
        migrations.RunPython(
            enable_schema_assertions,
            reverse_code=disable_schema_assertions,
        ),
    ]
