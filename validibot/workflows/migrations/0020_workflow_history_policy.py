from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0019_org_guest_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflow",
            name="history_policy",
            field=models.CharField(
                choices=[
                    ("versioned", "Versioned history"),
                    ("mutable", "Mutable history"),
                ],
                default="versioned",
                help_text=(
                    "Controls whether this workflow preserves historical run "
                    "reproducibility by requiring new versions for semantic edits, "
                    "or allows in-place changes after runs."
                ),
                max_length=16,
            ),
        ),
    ]
