"""Mark ``Workflow.version`` as required (``blank=False``, default ``"1"``).

This is the schema half of the "version is required" change. The data
half — backfilling existing empty rows — runs in the preceding migration
``0023_backfill_empty_workflow_versions``. Splitting them means the
schema only tightens after every row in the table carries a real label.

After this migration:

  - the field rejects empty values at the form / ``full_clean()`` layer
    via the updated ``validate_workflow_version``;
  - new rows created without an explicit ``version`` get ``"1"`` rather
    than the previous empty default;
  - the ``(org, slug, version)`` unique constraint can rely on every row
    carrying a non-empty label, which is what the version-history panel,
    "latest" resolver, and clone arithmetic all assume.
"""

from django.db import migrations
from django.db import models

import validibot.workflows.models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0023_backfill_empty_workflow_versions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflow",
            name="version",
            field=models.CharField(
                default="1",
                help_text=(
                    "Version identifier (integer or semantic version with major, "
                    "minor, and patch numbers, e.g., '1' or '1.0.0'). Required: "
                    "every workflow row must carry a label so the family identity "
                    "``(org, slug, version)`` stays unique and 'latest version' "
                    "resolution stays deterministic."
                ),
                max_length=40,
                validators=[validibot.workflows.models.validate_workflow_version],
            ),
        ),
    ]
