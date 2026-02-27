"""
Backfill ``supports_assertions=True`` for validator types that support
step-level assertions.

Migration 0019 added the ``supports_assertions`` BooleanField with
``default=False``.  Existing validators of types that support assertions
(Basic, EnergyPlus, FMU, Custom, AI Assist, THERM) need to be set to
``True`` so the assertion UI and endpoints remain available for them.

System validators would eventually be corrected by ``sync_validators``,
but user-created Custom and FMU validators wouldn't — they must be
backfilled here.
"""

from django.db import migrations

# Validation types that support step-level assertions.
# Matches the ``supports_assertions=True`` declarations in
# ``builtin_configs.py`` and the per-package ``config.py`` files.
_ASSERTION_CAPABLE_TYPES = [
    "BASIC",
    "ENERGYPLUS",
    "FMU",
    "CUSTOM_VALIDATOR",
    "AI_ASSIST",
    "THERM",
]


def backfill_supports_assertions(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    updated = Validator.objects.filter(
        validation_type__in=_ASSERTION_CAPABLE_TYPES,
    ).update(supports_assertions=True)

    if updated:
        print(f"  Backfilled supports_assertions=True for {updated} validator(s).")


def reverse_backfill(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    Validator.objects.filter(
        validation_type__in=_ASSERTION_CAPABLE_TYPES,
    ).update(supports_assertions=False)


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0019_add_supports_assertions_to_validator"),
    ]

    operations = [
        migrations.RunPython(
            backfill_supports_assertions,
            reverse_code=reverse_backfill,
        ),
    ]
