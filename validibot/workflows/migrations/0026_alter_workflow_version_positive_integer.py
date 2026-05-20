"""Store ``Workflow.version`` as a positive integer.

The previous migration rewrites legacy string labels to integer strings with
family-level collision handling. This schema migration makes that invariant
explicit in the database-facing model field.
"""

from django.core.validators import MinValueValidator
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0025_collapse_workflow_versions_to_integers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflow",
            name="version",
            field=models.PositiveIntegerField(
                default=1,
                help_text=(
                    "Positive integer version number. Every workflow row must "
                    "carry a version so the family identity ``(org, slug, "
                    "version)`` stays unique and 'latest version' resolution "
                    "stays deterministic."
                ),
                validators=[MinValueValidator(1)],
            ),
        ),
    ]
