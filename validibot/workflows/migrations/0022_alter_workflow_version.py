from django.db import migrations
from django.db import models

import validibot.workflows.models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0021_normalize_workflow_version_labels"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflow",
            name="version",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Version identifier (integer or semantic version with major, "
                    "minor, and patch numbers, e.g., '1' or '1.0.0')."
                ),
                max_length=40,
                validators=[validibot.workflows.models.validate_workflow_version],
            ),
        ),
    ]
