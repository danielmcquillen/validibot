"""
Backfill SignalDefinition + StepSignalBinding rows from existing template configs.

EnergyPlus steps with parameterized templates store variable metadata in
``step.config["template_variables"]`` as a JSON list. This migration creates
corresponding ``SignalDefinition`` (step-owned, origin_kind=TEMPLATE) and
``StepSignalBinding`` rows.

All logic is inlined for migration hermeticity.

Idempotent: uses ``get_or_create`` keyed on
``(workflow_step, contract_key, direction)`` so re-running is safe.
"""

from django.db import migrations
from slugify import slugify


_TEMPLATE_TYPE_MAP = {
    "number": "number",
    "choice": "string",
    "text": "string",
}


def _migrate_step_template_variables(apps, _schema_editor):
    """Create SignalDefinition + StepSignalBinding rows from template config."""
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")
    SignalDefinition = apps.get_model("validations", "SignalDefinition")
    StepSignalBinding = apps.get_model("validations", "StepSignalBinding")

    steps = WorkflowStep.objects.exclude(
        config={},
    ).exclude(config__isnull=True)
    migrated = 0

    for step in steps.iterator():
        template_vars = (step.config or {}).get("template_variables", [])
        if not template_vars:
            continue

        batch_keys: set[str] = set()

        for var in template_vars:
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

            variable_type = var.get("variable_type", "text")
            data_type = _TEMPLATE_TYPE_MAP.get(variable_type, "string")

            sig, _created = SignalDefinition.objects.get_or_create(
                workflow_step=step,
                contract_key=contract_key,
                direction="input",
                defaults={
                    "native_name": name,
                    "label": var.get("description") or "",
                    "description": "",
                    "data_type": data_type,
                    "unit": var.get("units") or "",
                    "origin_kind": "template",
                    "provider_binding": {
                        "variable_type": variable_type,
                    },
                    "metadata": {
                        "variable_type": variable_type,
                        "min_value": var.get("min_value"),
                        "min_exclusive": var.get("min_exclusive", False),
                        "max_value": var.get("max_value"),
                        "max_exclusive": var.get("max_exclusive", False),
                        "choices": var.get("choices", []),
                    },
                },
            )

            default = var.get("default")
            StepSignalBinding.objects.get_or_create(
                workflow_step=step,
                signal_definition=sig,
                defaults={
                    "source_scope": "submission_payload",
                    "source_data_path": name,
                    "default_value": default,
                    "is_required": default is None or default == "",
                },
            )

        migrated += 1

    if migrated:
        print(f"  Migrated template signals for {migrated} step(s)")


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0028_migrate_step_fmu_to_signal_definitions"),
        ("workflows", "0007_signal_definition_models"),
    ]

    operations = [
        migrations.RunPython(
            _migrate_step_template_variables,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
