"""Backfill source_scope for existing signal-prefixed bindings.

Any StepSignalBinding whose source_data_path starts with "s." was
pointing at a workflow signal but was stored with source_scope =
"submission_payload" (the old default). This migration updates those
rows to source_scope = "signal" and strips the "s." prefix from the
path so the resolver finds the value in the workflow signals namespace.
"""

from django.db import migrations


def backfill_signal_scope(apps, schema_editor):
    StepSignalBinding = apps.get_model("validations", "StepSignalBinding")
    bindings = StepSignalBinding.objects.filter(
        source_data_path__startswith="s.",
        source_scope="submission_payload",
    )
    updated = 0
    for binding in bindings:
        binding.source_scope = "signal"
        binding.source_data_path = binding.source_data_path[2:]  # strip "s."
        binding.save(update_fields=["source_scope", "source_data_path"])
        updated += 1
    if updated:
        print(f"  Backfilled {updated} signal binding(s) to SIGNAL scope.")


def reverse_backfill(apps, schema_editor):
    StepSignalBinding = apps.get_model("validations", "StepSignalBinding")
    bindings = StepSignalBinding.objects.filter(source_scope="signal")
    for binding in bindings:
        binding.source_scope = "submission_payload"
        binding.source_data_path = f"s.{binding.source_data_path}"
        binding.save(update_fields=["source_scope", "source_data_path"])


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0039_add_signal_binding_source_scope"),
    ]

    operations = [
        migrations.RunPython(backfill_signal_scope, reverse_backfill),
    ]
