# Generated manually for ADR-2026-01-07: Guest Management and Workflow Visibility
"""
Rename make_info_public to make_info_page_public and add is_public field.

make_info_page_public: Controls whether the workflow's info page is publicly visible
is_public: Controls whether any authenticated user can launch the workflow
"""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0020_alter_workflow_data_retention"),
    ]

    operations = [
        # Rename the existing field to clarify its purpose
        migrations.RenameField(
            model_name="workflow",
            old_name="make_info_public",
            new_name="make_info_page_public",
        ),
        # Add the new is_public field for execution visibility
        migrations.AddField(
            model_name="workflow",
            name="is_public",
            field=models.BooleanField(
                default=False,
                help_text="If true, any authenticated user can launch this workflow.",
            ),
        ),
    ]
