"""Persist URI-free strict input evidence on execution attempts."""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    """Add the launch-time evidence snapshot used by portable manifests."""

    dependencies = [
        ("validations", "0027_artifact_storage_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="executionattempt",
            name="input_evidence_snapshot",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "URI-free strict input identities and source relationships "
                    "committed with the attempt. Never contains callback credentials."
                ),
            ),
        ),
    ]
