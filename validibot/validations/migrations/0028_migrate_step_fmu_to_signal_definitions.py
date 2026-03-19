"""
Backfill SignalDefinition + StepSignalBinding rows from existing step FMU configs.

Steps that have an FMU uploaded store variable metadata in
``step.config["fmu_variables"]`` as a JSON list. This migration creates
corresponding ``SignalDefinition`` (step-owned, origin_kind=FMU) and
``StepSignalBinding`` rows so the new signal model has complete data.

All logic is inlined rather than importing from runtime service modules
to ensure the migration remains hermetic — independent of future changes
to the service layer's interface or semantics.

Idempotent: uses ``get_or_create`` keyed on
``(workflow_step, contract_key, direction)`` so re-running is safe.
"""

from django.db import migrations
from slugify import slugify


_FMU_VALUE_TYPE_MAP = {
    "real": "number",
    "integer": "number",
    "enumeration": "number",
    "boolean": "boolean",
    "string": "string",
}


def _migrate_step_fmu_variables(apps, _schema_editor):
    """Create SignalDefinition + StepSignalBinding rows from step config JSON.

    Inlined to avoid depending on runtime service code that may change
    after this migration is written.
    """
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")
    SignalDefinition = apps.get_model("validations", "SignalDefinition")
    StepSignalBinding = apps.get_model("validations", "StepSignalBinding")

    steps_with_fmu = WorkflowStep.objects.exclude(
        config={},
    ).exclude(config__isnull=True)
    migrated = 0

    for step in steps_with_fmu.iterator():
        fmu_vars = (step.config or {}).get("fmu_variables", [])
        if not fmu_vars:
            continue

        batch_keys: set[str] = set()

        for var in fmu_vars:
            causality = (var.get("causality") or "").lower()
            if causality not in ("input", "output"):
                continue

            name = var.get("name", "")
            if not name:
                continue

            base_key = slugify(name, separator="_") or "signal"
            contract_key = base_key
            counter = 2
            while contract_key in batch_keys:
                contract_key = f"{base_key}_{counter}"
                counter += 1
            batch_keys.add(contract_key)

            vt = (var.get("value_type") or "").lower()
            data_type = _FMU_VALUE_TYPE_MAP.get(vt, "object")

            sig, _created = SignalDefinition.objects.get_or_create(
                workflow_step=step,
                contract_key=contract_key,
                direction=causality,
                defaults={
                    "native_name": name,
                    "label": var.get("label") or "",
                    "description": var.get("description") or "",
                    "data_type": data_type,
                    "unit": var.get("unit") or "",
                    "origin_kind": "fmu",
                    "provider_binding": {
                        "causality": causality,
                    },
                    "metadata": {
                        "variability": var.get("variability", ""),
                        "value_reference": var.get("value_reference", 0),
                        "value_type": var.get("value_type", ""),
                    },
                },
            )

            if causality == "input":
                StepSignalBinding.objects.get_or_create(
                    workflow_step=step,
                    signal_definition=sig,
                    defaults={
                        "source_scope": "submission_payload",
                        "source_data_path": name,
                        "is_required": True,
                    },
                )

        migrated += 1

    if migrated:
        print(f"  Migrated FMU signals for {migrated} step(s)")


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0027_migrate_catalog_to_signal_definitions"),
        ("workflows", "0007_signal_definition_models"),
    ]

    operations = [
        migrations.RunPython(
            _migrate_step_fmu_variables,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
