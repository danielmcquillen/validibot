"""
Migrate ``config["resource_file_ids"]`` → ``WorkflowStepResource`` rows.

Phase 0 of the EnergyPlus Parameterized Templates ADR replaces the JSON
list of UUID strings in ``WorkflowStep.config["resource_file_ids"]`` with
proper FK rows in the new ``WorkflowStepResource`` through table.

Forward
-------
For every ``WorkflowStep`` whose ``config`` JSON contains a
``resource_file_ids`` list, this migration creates one
``WorkflowStepResource`` row per UUID:

- ``role = "WEATHER_FILE"`` (all existing resource_file_ids are weather files)
- ``validator_resource_file`` FK → the ``ValidatorResourceFile`` with that UUID

Unresolvable UUIDs (no matching ``ValidatorResourceFile``) are logged as
warnings and skipped — they represent stale references that can't be
migrated.

Reverse
-------
Deletes ``WorkflowStepResource`` rows with ``role = "WEATHER_FILE"`` that
were created by this migration (those with a non-null
``validator_resource_file``).

Note: The stale ``resource_file_ids`` key remains in the JSON config after
this migration. That's harmless because ``BaseStepConfig`` uses
``extra="allow"``, so Pydantic silently ignores unknown keys.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

WEATHER_FILE_ROLE = "WEATHER_FILE"


def forwards(apps, schema_editor):
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")
    WorkflowStepResource = apps.get_model("workflows", "WorkflowStepResource")
    ValidatorResourceFile = apps.get_model("validations", "ValidatorResourceFile")

    # All existing resource_file_ids entries are weather files for EnergyPlus steps.
    steps_with_resources = WorkflowStep.objects.exclude(config={}).exclude(config__isnull=True)

    created_count = 0
    skipped_count = 0

    for step in steps_with_resources:
        config = step.config or {}
        resource_file_ids = config.get("resource_file_ids", [])
        if not resource_file_ids:
            continue

        for uuid_str in resource_file_ids:
            if not uuid_str:
                continue

            try:
                vrf = ValidatorResourceFile.objects.get(pk=uuid_str)
            except ValidatorResourceFile.DoesNotExist:
                logger.warning(
                    "Skipping stale resource_file_id=%s on WorkflowStep pk=%s "
                    "(no matching ValidatorResourceFile).",
                    uuid_str,
                    step.pk,
                )
                skipped_count += 1
                continue

            # Avoid duplicates if migration is re-run
            if not WorkflowStepResource.objects.filter(
                step=step,
                role=WEATHER_FILE_ROLE,
                validator_resource_file=vrf,
            ).exists():
                WorkflowStepResource.objects.create(
                    step=step,
                    role=WEATHER_FILE_ROLE,
                    validator_resource_file=vrf,
                )
                created_count += 1

    if created_count or skipped_count:
        print(
            f"  Migrated resource_file_ids → WorkflowStepResource: "
            f"{created_count} created, {skipped_count} skipped (stale)."
        )


def reverse(apps, schema_editor):
    WorkflowStepResource = apps.get_model("workflows", "WorkflowStepResource")

    # Only delete catalog-reference weather files (those created by the
    # forward migration). Step-owned files are not affected.
    deleted_count, _ = WorkflowStepResource.objects.filter(
        role=WEATHER_FILE_ROLE,
        validator_resource_file__isnull=False,
    ).delete()

    if deleted_count:
        print(f"  Reversed: deleted {deleted_count} WorkflowStepResource row(s).")


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0022_workflowstepresource"),
        ("workflows", "0002_workflowstepresource"),
    ]

    operations = [
        migrations.RunPython(
            forwards,
            reverse_code=reverse,
        ),
    ]
