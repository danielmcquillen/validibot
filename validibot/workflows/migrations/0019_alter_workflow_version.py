"""
Add version validator and help text to Workflow.version field.

This migration applies the version format validation from ADR-2026-01-06.
"""

import validibot.workflows.models
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0018_backfill_empty_slugs_versions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflow",
            name="version",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Version identifier (integer or semantic version, "
                "e.g., '1' or '1.0.0').",
                max_length=40,
                validators=[validibot.workflows.models.validate_workflow_version],
            ),
        ),
    ]
