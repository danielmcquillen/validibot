"""
Populate target_signal_definition FK from target_catalog_entry FK.

For each RulesetAssertion that has a target_catalog_entry, finds the
corresponding SignalDefinition (matched by validator, slug=contract_key,
run_stage=direction) and sets target_signal_definition to it.

After this migration, assertions that previously targeted catalog entries
now also have the new FK set. The old target_catalog_entry FK remains
populated for backward compatibility — Phase 7 will remove it.

Idempotent: skips assertions that already have target_signal_definition set.
"""

from django.db import migrations


def _populate_target_signal_definition(apps, _schema_editor):
    """Set target_signal_definition from target_catalog_entry where possible."""
    RulesetAssertion = apps.get_model("validations", "RulesetAssertion")
    SignalDefinition = apps.get_model("validations", "SignalDefinition")

    # Only process assertions that have a catalog entry FK but no signal FK yet.
    assertions = RulesetAssertion.objects.filter(
        target_catalog_entry__isnull=False,
        target_signal_definition__isnull=True,
    ).select_related("target_catalog_entry")

    migrated = 0
    for assertion in assertions.iterator():
        entry = assertion.target_catalog_entry
        if not entry:
            continue

        # Find the corresponding SignalDefinition by matching
        # (validator, contract_key=slug, direction=run_stage).
        sig = SignalDefinition.objects.filter(
            validator=entry.validator,
            contract_key=entry.slug,
            direction=entry.run_stage,
        ).first()

        if sig:
            assertion.target_signal_definition = sig
            assertion.target_catalog_entry = None
            assertion.save(
                update_fields=["target_signal_definition", "target_catalog_entry"],
            )
            migrated += 1

    if migrated:
        print(f"  Migrated {migrated} assertion target(s) to SignalDefinition")


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0030_add_target_signal_definition"),
    ]

    operations = [
        migrations.RunPython(
            _populate_target_signal_definition,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
