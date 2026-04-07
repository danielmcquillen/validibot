"""Backfill source_scope for existing signal-prefixed bindings.

Any StepSignalBinding whose source_data_path starts with "s." or
"signal." was pointing at a workflow signal but was stored with
source_scope = "submission_payload" (the old default).  Similarly,
rows starting with "payload." have a redundant prefix that the
resolver cannot navigate (it would look for a literal "payload" key).

This migration:
- "s.<name>" / "signal.<name>" → scope = SIGNAL, path = <name>
- "payload.<path>" → scope unchanged, path = <path> (strip prefix)
"""

from django.db import migrations


def backfill_signal_scope(apps, schema_editor):
    StepSignalBinding = apps.get_model("validations", "StepSignalBinding")
    updated = 0

    # s. prefix → SIGNAL scope
    for binding in StepSignalBinding.objects.filter(
        source_data_path__startswith="s.",
        source_scope="submission_payload",
    ):
        binding.source_scope = "signal"
        binding.source_data_path = binding.source_data_path[2:]
        binding.save(update_fields=["source_scope", "source_data_path"])
        updated += 1

    # signal. prefix → SIGNAL scope
    for binding in StepSignalBinding.objects.filter(
        source_data_path__startswith="signal.",
        source_scope="submission_payload",
    ):
        binding.source_scope = "signal"
        binding.source_data_path = binding.source_data_path[7:]
        binding.save(update_fields=["source_scope", "source_data_path"])
        updated += 1

    # payload. prefix → strip redundant prefix (scope stays the same)
    for binding in StepSignalBinding.objects.filter(
        source_data_path__startswith="payload.",
        source_scope="submission_payload",
    ):
        binding.source_data_path = binding.source_data_path[8:]
        binding.save(update_fields=["source_data_path"])
        updated += 1

    if updated:
        print(f"  Backfilled {updated} signal binding(s).")


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
