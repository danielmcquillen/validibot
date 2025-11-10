from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0011_catalog_run_stage"),
    ]

    operations = [
        migrations.AddField(
            model_name="validator",
            name="processor_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The name of the process that generates output signals from input signals.",
                max_length=200,
            ),
        ),
    ]
