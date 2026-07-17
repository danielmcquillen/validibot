"""Persist immutable provider versions for run artifacts."""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    """Add the storage-version half of the strict artifact identity."""

    dependencies = [
        ("validations", "0026_current_schema"),
    ]

    operations = [
        migrations.AddField(
            model_name="artifact",
            name="storage_version",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Provider-specific immutable object version recorded by "
                    "the validator output envelope."
                ),
                max_length=512,
            ),
        ),
    ]
