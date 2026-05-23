"""Add StepIODefinition.on_missing field.

Per ADR-2026-05-22 and the May 2026 code review's P3 finding: the
``on_missing`` policy is captured on ``CatalogEntrySpec`` but the
``sync_validators`` command was discarding it because the underlying
``StepIODefinition`` model had no field for it. This migration adds the
column so the catalog's intent is persisted to the database row.

The field defaults to "null" — the safe behaviour ("inject null;
assertions guard with has() or != null"). Existing rows get this
default on the migration; subsequent ``sync_validators`` runs will
update each row to match its catalog spec.

**Runtime enforcement remains deferred.** Capturing the field doesn't
make the runtime consult it — that's a separate follow-up PR. The
field is here so future runtime work has a stable place to read intent
from.
"""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0051_rename_signal_name_to_promoted_signal_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="stepiodefinition",
            name="on_missing",
            field=models.CharField(
                choices=[
                    ("error", "Fail the run with a clear message"),
                    (
                        "null",
                        "Inject null; assertions guard with has() or != null",
                    ),
                    ("ignore", "Omit silently; references resolve to null"),
                ],
                default="null",
                help_text=(
                    "Behaviour when the value cannot be resolved. Default "
                    "'null' is the safe choice; 'error' for entries "
                    "downstream assertions reliably depend on; 'ignore' "
                    "for genuinely optional facts. Runtime enforcement is "
                    "deferred — the field is captured now so future PRs "
                    "can read intent."
                ),
                max_length=10,
            ),
        ),
    ]
