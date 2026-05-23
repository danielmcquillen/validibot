"""Rename SignalDefinition → StepIODefinition + StepSignalBinding → StepInputBinding.

Per ADR-2026-05-22b's terminology cleanup ("signal" is reserved for the
workflow vocabulary; step inputs and step outputs are step-local
catalog entries):

- ``SignalDefinition`` model is renamed to ``StepIODefinition``
- ``StepSignalBinding`` model is renamed to ``StepInputBinding``
- ``SignalDefinition.signal_name`` field is renamed to
  ``StepIODefinition.promoted_signal_name``

The class renames preserve the existing database table names. The
migration wraps both ``RenameModel`` and ``AlterModelTable`` inside
``SeparateDatabaseAndState`` with empty ``database_operations`` so
Django updates its migration-state graph WITHOUT issuing any
``ALTER TABLE`` for the class renames. Only the column rename in step
2 issues real DDL.

Without the AlterModelTable in the state, the subsequent
``RenameField`` operation would compute the target table from the
renamed class name (``validations_stepiodefinition``) and fail
because the actual table is still ``validations_signaldefinition``.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0050_schema_validators_support_assertions"),
    ]

    operations = [
        # 1. State-only updates: rename the model classes AND record
        #    the preserved db_table override. No DB changes here — the
        #    underlying tables (validations_signaldefinition and
        #    validations_stepsignalbinding) stay put.
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RenameModel(
                    old_name="SignalDefinition",
                    new_name="StepIODefinition",
                ),
                migrations.RenameModel(
                    old_name="StepSignalBinding",
                    new_name="StepInputBinding",
                ),
                migrations.AlterModelTable(
                    name="stepiodefinition",
                    table="validations_signaldefinition",
                ),
                migrations.AlterModelTable(
                    name="stepinputbinding",
                    table="validations_stepsignalbinding",
                ),
            ],
        ),
        # 2. Rename the column — this IS real DDL. With the state's
        #    db_table set correctly above, Django generates:
        #      ALTER TABLE "validations_signaldefinition"
        #        RENAME COLUMN "signal_name" TO "promoted_signal_name"
        migrations.RenameField(
            model_name="stepiodefinition",
            old_name="signal_name",
            new_name="promoted_signal_name",
        ),
    ]
