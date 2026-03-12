"""
Enable ``allow_custom_assertion_targets`` for FMU validators.

FMU validators produce output signals whose names come from the FMU's
``modelDescription.xml`` (e.g. ``T_room``, ``Q_cooling_actual``), not from
a pre-defined validator catalog.  Workflow authors write CEL assertions
referencing those variable names directly (``T_room < 300.15``).

For CEL evaluation, ``_build_cel_context()`` only exposes payload keys
as top-level CEL variables when ``allow_custom_assertion_targets`` is
True.  Without this flag, FMU output variable names are invisible to
CEL expressions, causing "undefined name" errors.

This applies to both system FMU validators and user-created ones.
System validators would eventually be corrected by
``create_default_validators``, but user-created FMU validators must be
backfilled here.
"""

from django.db import migrations


def enable_custom_targets_for_fmu(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    updated = Validator.objects.filter(
        validation_type="FMU",
        allow_custom_assertion_targets=False,
    ).update(allow_custom_assertion_targets=True)
    if updated:
        print(
            f"  Enabled allow_custom_assertion_targets for {updated} FMU validator(s)."
        )


def reverse(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    Validator.objects.filter(
        validation_type="FMU",
    ).update(allow_custom_assertion_targets=False)


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0024_agent_access_fields"),
    ]

    operations = [
        migrations.RunPython(
            enable_custom_targets_for_fmu,
            reverse_code=reverse,
        ),
    ]
