"""
Data migration: populate SignalDefinition, Derivation, and StepSignalBinding
from existing ValidatorCatalogEntry rows.

Also generates step_key values for existing WorkflowStep rows.

This migration runs AFTER the schema migration (0026) that created the
new tables. It copies data from ValidatorCatalogEntry into the new models
without deleting the old rows — both old and new coexist during the
transition period. The old model is removed in a later cleanup phase.

Implements the unified signal model and data path resolution.
"""

from django.db import migrations
from slugify import slugify


def _generate_step_keys(apps, _schema_editor):
    """Auto-generate step_key for all existing WorkflowStep rows.

    Uses slugify on the step name, with numeric suffixes to handle
    collisions within the same workflow. Steps without names get
    'step' as the base key.
    """
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")

    # Group steps by workflow to handle uniqueness within each workflow
    workflow_ids = (
        WorkflowStep.objects.values_list("workflow_id", flat=True).distinct()
    )

    for workflow_id in workflow_ids:
        steps = WorkflowStep.objects.filter(
            workflow_id=workflow_id,
        ).order_by("order")
        used_keys = set()

        for step in steps:
            if step.step_key:
                used_keys.add(step.step_key)
                continue

            base_key = slugify(step.name, separator="_") if step.name else "step"
            base_key = base_key or "step"
            candidate = base_key
            counter = 2
            while candidate in used_keys:
                candidate = f"{base_key}_{counter}"
                counter += 1
            used_keys.add(candidate)
            step.step_key = candidate
            step.save(update_fields=["step_key"])


def _migrate_catalog_entries_to_signals(apps, _schema_editor):
    """Copy ValidatorCatalogEntry (SIGNAL) rows to SignalDefinition.

    Maps fields per the unified signal model:
    - slug → contract_key
    - slug → native_name (same initially; provider-specific later)
    - run_stage → direction
    - target_data_path → (stored temporarily; moved to binding below)
    - entry_type filtering: only SIGNAL rows here
    """
    ValidatorCatalogEntry = apps.get_model(
        "validations",
        "ValidatorCatalogEntry",
    )
    SignalDefinition = apps.get_model("validations", "SignalDefinition")

    for entry in ValidatorCatalogEntry.objects.filter(entry_type="signal"):
        SignalDefinition.objects.get_or_create(
            validator=entry.validator,
            contract_key=entry.slug,
            direction=entry.run_stage,
            defaults={
                "native_name": entry.slug,
                "label": entry.label,
                "description": entry.description,
                "data_type": entry.data_type,
                "origin_kind": "catalog",
                "order": entry.order,
                "is_hidden": entry.is_hidden,
                "unit": (entry.metadata or {}).get("units", ""),
                "provider_binding": entry.binding_config or {},
                "metadata": entry.metadata or {},
            },
        )


def _migrate_catalog_entries_to_derivations(apps, _schema_editor):
    """Copy ValidatorCatalogEntry (DERIVATION) rows to Derivation.

    The CEL expression moves from binding_config["expr"] to the
    dedicated expression field.
    """
    ValidatorCatalogEntry = apps.get_model(
        "validations",
        "ValidatorCatalogEntry",
    )
    Derivation = apps.get_model("validations", "Derivation")

    for entry in ValidatorCatalogEntry.objects.filter(entry_type="derivation"):
        binding = entry.binding_config or {}
        expression = binding.get("expr", "")

        Derivation.objects.get_or_create(
            validator=entry.validator,
            contract_key=entry.slug,
            defaults={
                "expression": expression,
                "label": entry.label,
                "description": entry.description,
                "data_type": entry.data_type,
                "order": entry.order,
                "is_hidden": entry.is_hidden,
                "unit": (entry.metadata or {}).get("units", ""),
                "metadata": entry.metadata or {},
            },
        )


def _create_step_signal_bindings(apps, _schema_editor):
    """Create StepSignalBinding rows for existing steps using library validators.

    For each WorkflowStep that has a validator with SignalDefinition rows,
    create a StepSignalBinding with default values. The source_data_path
    is populated from the old target_data_path on the ValidatorCatalogEntry.
    """
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")
    SignalDefinition = apps.get_model("validations", "SignalDefinition")
    StepSignalBinding = apps.get_model("validations", "StepSignalBinding")
    ValidatorCatalogEntry = apps.get_model(
        "validations",
        "ValidatorCatalogEntry",
    )

    for step in WorkflowStep.objects.filter(validator__isnull=False):
        # Get input signal definitions for this step's validator
        input_signals = SignalDefinition.objects.filter(
            validator=step.validator,
            direction="input",
        )

        for sig_def in input_signals:
            # Look up the old catalog entry to get target_data_path
            old_entry = ValidatorCatalogEntry.objects.filter(
                validator=step.validator,
                slug=sig_def.contract_key,
                run_stage="input",
                entry_type="signal",
            ).first()

            source_path = ""
            is_required = True
            default_value = None

            if old_entry:
                source_path = old_entry.target_data_path or ""
                is_required = old_entry.is_required
                default_value = old_entry.default_value

            StepSignalBinding.objects.get_or_create(
                workflow_step=step,
                signal_definition=sig_def,
                defaults={
                    "source_scope": "submission_payload",
                    "source_data_path": source_path,
                    "default_value": default_value,
                    "is_required": is_required,
                },
            )


def _noop(apps, schema_editor):
    """Reverse migration is a no-op — data stays in the new tables."""


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0026_signal_definition_models"),
        ("workflows", "0007_signal_definition_models"),
    ]

    operations = [
        migrations.RunPython(
            _generate_step_keys,
            _noop,
            elidable=True,
        ),
        migrations.RunPython(
            _migrate_catalog_entries_to_signals,
            _noop,
            elidable=True,
        ),
        migrations.RunPython(
            _migrate_catalog_entries_to_derivations,
            _noop,
            elidable=True,
        ),
        migrations.RunPython(
            _create_step_signal_bindings,
            _noop,
            elidable=True,
        ),
    ]
