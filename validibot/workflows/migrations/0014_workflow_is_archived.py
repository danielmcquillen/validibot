from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0013_remove_workflow_allow_meta_data_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflow",
            name="is_archived",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Archived workflows are disabled and hidden unless explicitly shown."
                ),
            ),
        ),
    ]
